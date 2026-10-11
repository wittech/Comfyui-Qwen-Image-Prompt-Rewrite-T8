import copy
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from jinja2.sandbox import ImmutableSandboxedEnvironment

import pe_runtime
from pe_runtime import LocalRuntime, local_models, parse_answer, vlm_module


class ProcessorList(list):
    def __call__(self, tokens, scores):
        for processor in self:
            scores = processor(tokens, scores)
        return scores


class TextFormatter:
    """Render the supplied GGUF template using real Jinja, with no vision API."""

    def __init__(self, template, eos_token, bos_token, stop_token_ids):
        self.template = ImmutableSandboxedEnvironment().from_string(template)
        self.eos_token = eos_token
        self.bos_token = bos_token

    def to_chat_handler(self):
        def complete(*, llama, messages):
            prompt = self.template.render(messages=messages, add_generation_prompt=True,
                                          eos_token=self.eos_token, bos_token=self.bos_token)
            llama.prompts.append(prompt)
            return llama.result()
        return complete


class Model:
    def __init__(self):
        self.closed = False
        self.calls = []
        self.thinking = []
        self.penalties = []
        self.prompts = []
        self.chat_handler = None
        self.metadata = {"tokenizer.chat_template": (
            "{% for message in messages %}{{ '<|im_start|>' + message.role + '\\n' "
            "+ message.content + eos_token + '\\n' }}{% endfor %}"
            "{% if add_generation_prompt %}{{ '<|im_start|>assistant\\n' }}"
            "{% if enable_thinking %}{{ '<think>\\n' }}"
            "{% else %}{{ '<think>\\n\\n</think>\\n\\n' }}{% endif %}{% endif %}")}
        self.response = '{"rewritten_prompt":"A blue dog.","wh_ratio":"1:1"}'

    def close(self):
        self.closed = True

    def reset(self):
        if self.closed:
            raise RuntimeError("Use after close")

    def token_eos(self):
        return 2

    def token_bos(self):
        return -1

    def detokenize(self, tokens, special=False):
        assert tokens == [2] and special
        return b"<|im_end|>"

    def result(self):
        return {"choices": [{"message": {"content": self.response},
                             "finish_reason": "stop"}], "usage": {}}

    # Deliberately match the native API, without HTTP-only template kwargs.
    def create_chat_completion(self, messages, seed, stream, max_tokens,
                               temperature, top_p, logits_processor, top_k=40,
                               min_p=0.05, present_penalty=0.0):
        logits_processor([], [])
        self.calls.append(copy.deepcopy(messages))
        self.penalties.append(present_penalty)
        if callable(self.chat_handler):
            return self.chat_handler(llama=self, messages=messages)
        self.thinking.append((self.handler.enable_thinking,
                              self.handler.extra_template_arguments["enable_thinking"]))
        return self.result()


class Storage:
    def __init__(self):
        self.llm = self.chat_handler = self.current_config = None
        self.loads = []
        self.on_load = None

    def clean(self, all=False):
        if self.llm:
            self.llm.close()
        self.llm = self.chat_handler = self.current_config = None

    def load_model(self, config):
        self.clean(all=True)
        self.current_config = config.copy()
        self.loads.append(config.copy())
        # Match upstream: a named Qwen vision handler cannot be constructed
        # without a projector. Previously this fixture accepted that bad config.
        if (config.get("chat_handler", "None") != "None"
                and config.get("mmproj", "None") == "None"):
            raise ValueError("Qwen35ChatHandler(__init__): `mmproj_path` is required")
        self.chat_handler = (types.SimpleNamespace(
            enable_thinking=True, extra_template_arguments={"enable_thinking": True})
            if config.get("chat_handler", "None") != "None" else None)
        self.llm = Model()
        self.llm.handler = self.chat_handler
        self.llm.chat_handler = self.chat_handler
        if self.on_load:
            self.on_load()

    def ensure_embedding_mode(self, enabled):
        assert enabled is False


class LinuxBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.model = Path(self.temp.name) / "model.gguf"
        self.vision = Path(self.temp.name) / "model.mmproj.gguf"
        self.model.write_bytes(b"model")
        self.vision.write_bytes(b"vision")
        self.storage = Storage()
        module = types.ModuleType("custom_nodes.renamed_vlm.nodes")
        module.LLAMA_CPP_STORAGE = self.storage
        module.Qwen35ChatHandler = object
        module.llama_cpp = types.SimpleNamespace(
            LogitsProcessorList=ProcessorList,
            llama_chat_format=types.SimpleNamespace(Jinja2ChatFormatter=TextFormatter))
        loader = type("llama_cpp_model_loader", (), {"__module__": module.__name__})
        nodes = types.ModuleType("nodes")
        nodes.NODE_CLASS_MAPPINGS = {"llama_cpp_model_loader": loader}
        self.module = module
        for context in (
            patch.dict(sys.modules, {module.__name__: module, "nodes": nodes}),
            patch.object(sys, "platform", "linux"),
            patch.object(pe_runtime, "check_interrupt"),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.runtime = LocalRuntime()
        self.addCleanup(self.runtime.stop)

    def start(self, vision=None, timeout=900):
        self.runtime.start(self.model, vision, 24576, -1, startup_timeout=timeout)

    def payload(self, images=(), thinking=True):
        return {
            "model": "qwen-pe", "messages": [{"role": "user", "content": [
                *[{"type": "image_url", "image_url": {"url": value}} for value in images],
                {"type": "text", "text": "Make it blue."}]}],
            "seed": 42, "stream": False, "max_tokens": 100,
            "temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
            "presence_penalty": 1.5, "chat_template_kwargs": {"enable_thinking": thinking},
        }

    def test_rejects_windows_and_macos_without_loading(self):
        for platform in ("win32", "darwin"):
            with patch.object(sys, "platform", platform):
                with self.assertRaisesRegex(RuntimeError, "Linux only"):
                    self.start()
        self.assertEqual(self.storage.loads, [])

    def test_dependency_discovery_uses_registered_module_even_if_renamed(self):
        self.assertIs(vlm_module(), self.module)
        with patch.dict(sys.modules, {"nodes": None}):
            with self.assertRaisesRegex(RuntimeError, "ComfyUI-llama-cpp_vlm is not loaded"):
                self.start()
        self.module.Qwen35ChatHandler = None
        with self.assertRaisesRegex(RuntimeError, "Qwen35ChatHandler"):
            self.start()

    def test_native_load_reuses_then_switches_and_unloads(self):
        self.start()
        first = self.runtime.llm
        self.start()
        self.assertIs(self.runtime.llm, first)
        self.assertEqual(len(self.storage.loads), 1)
        self.start(self.vision)
        self.assertTrue(first.closed)
        config = self.storage.current_config
        self.assertEqual(config["model"], str(self.model.resolve()))
        self.assertEqual(config["mmproj"], str(self.vision.resolve()))
        self.assertEqual(config["chat_handler"], "Qwen3.5-Thinking")
        self.assertEqual(config["image_min_tokens"], 1024)
        second = self.runtime.llm
        self.runtime.stop()
        self.assertTrue(second.closed)
        self.assertIsNone(self.storage.llm)

    def test_text_only_loading_does_not_construct_a_vision_handler(self):
        self.start()
        self.assertEqual(self.storage.current_config["chat_handler"], "None")
        self.assertEqual(self.storage.current_config["mmproj"], "None")
        self.assertIsNone(self.storage.chat_handler)

    def test_text_inference_renders_GGUF_template_and_switches_thinking(self):
        self.start()
        llm = self.runtime.llm
        for thinking in (True, False, True):
            payload = self.payload(thinking=thinking)
            original = copy.deepcopy(payload)
            self.runtime._post_completion(payload, 10)
            ending = "<think>\n" if thinking else "<think>\n\n</think>\n\n"
            self.assertTrue(llm.prompts[-1].endswith(ending))
            self.assertIn("Make it blue.", llm.prompts[-1])
            self.assertEqual(llm.calls[-1][0]["content"], "Make it blue.")
            self.assertEqual(payload, original)
            self.assertIsNone(llm.chat_handler)
        self.assertEqual(len(self.storage.loads), 1)

    def test_text_only_rewrite_and_translation_run_without_mmproj(self):
        self.start()
        answer, _info = self.runtime.complete("t2i", "A blue dog.", [], 42, 10)
        self.assertEqual(answer["rewritten_prompt"], "A blue dog.")
        self.runtime.llm.response = "一只蓝色的狗。"
        translated, _usage = self.runtime._translate_prose("A blue dog.", "中文", 42, 10)
        self.assertEqual(translated, "一只蓝色的狗。")
        self.assertTrue(self.runtime.llm.prompts[-1].endswith("<think>\n\n</think>\n\n"))

    def test_text_path_rejects_image_content_and_missing_template(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "Image input requires"):
            self.runtime._post_completion(self.payload(["image-data"]), 10)
        self.assertIsNone(self.storage.llm)
        self.start()
        self.runtime.llm.metadata.clear()
        with self.assertRaisesRegex(RuntimeError, "missing tokenizer.chat_template"):
            self.runtime._post_completion(self.payload(), 10)
        self.assertIsNone(self.storage.llm)

    def test_reload_after_atomic_model_and_projector_replacement(self):
        self.start(self.vision)
        for path in (self.model, self.vision):
            previous = self.runtime.llm
            stat = path.stat()
            replacement = path.with_suffix(".new")
            replacement.write_bytes(b"x" * stat.st_size)
            os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            os.replace(replacement, path)
            self.start(self.vision)
            self.assertTrue(previous.closed)
            self.assertIsNot(self.runtime.llm, previous)

    def test_external_unload_and_replacement_do_not_use_stale_model(self):
        self.start()
        self.storage.clean(all=True)
        self.start()
        self.assertEqual(len(self.storage.loads), 2)
        self.storage.load_model({"model": "another-node.gguf"})
        external = self.storage.llm
        with self.assertRaisesRegex(RuntimeError, "unloaded/replaced"):
            self.runtime._post_completion(self.payload(), 10)
        self.runtime.stop()
        self.assertFalse(external.closed)
        self.assertIs(self.storage.llm, external)
        self.start()
        self.assertTrue(external.closed)

    def test_partial_load_failure_and_startup_budget_release_model(self):
        def fail():
            raise ValueError("bad projector")
        self.storage.on_load = fail
        with self.assertRaisesRegex(ValueError, "bad projector"):
            self.start(self.vision)
        self.assertIsNone(self.storage.llm)
        elapsed = [0]
        self.storage.on_load = lambda: elapsed.__setitem__(0, 61)
        with patch.object(pe_runtime.time, "monotonic", side_effect=lambda: elapsed[0]):
            with self.assertRaisesRegex(TimeoutError, "exceeded 60 seconds"):
                self.start(timeout=60)
        self.assertIsNone(self.storage.llm)
        self.assertIsNone(self.runtime.profile)

    def test_generation_preserves_image_order_and_restores_thinking(self):
        self.start(self.vision)
        payload = self.payload([f"image-{i}" for i in range(10)], thinking=False)
        original = copy.deepcopy(payload)
        llm = self.runtime.llm
        handler = self.storage.chat_handler
        self.runtime._post_completion(payload, 10)
        self.assertEqual(llm.calls[0], payload["messages"])
        self.assertEqual(payload, original)
        self.assertEqual(llm.thinking, [(False, False)])
        self.assertEqual(llm.penalties, [1.5])
        self.assertTrue(handler.enable_thinking)
        self.assertTrue(handler.extra_template_arguments["enable_thinking"])

    def test_generation_checks_interrupt_and_deadline_during_sampling(self):
        for error in (KeyboardInterrupt(), TimeoutError("generation timeout")):
            self.start()
            llm = self.runtime.llm
            with patch.object(pe_runtime, "check_interrupt", side_effect=[None, error]):
                with self.assertRaises(type(error)):
                    self.runtime._post_completion(self.payload(), 10)
            self.assertTrue(llm.closed)
            self.assertIsNone(self.storage.llm)
        self.start()
        with patch.object(pe_runtime.time, "monotonic", side_effect=[0, 0, 11]):
            with self.assertRaisesRegex(TimeoutError, "Native generation exceeded"):
                self.runtime._post_completion(self.payload(), 10)
        self.assertIsNone(self.storage.llm)

    def test_prefilled_thinking_cannot_expose_draft_json(self):
        self.start()
        draft = '{"rewritten_prompt":"draft","wh_ratio":"1:1"}'
        final = '{"rewritten_prompt":"A blue dog.","wh_ratio":"4:3"}'
        self.runtime.llm.response = draft + "</think>" + final
        result = self.runtime._post_completion(self.payload(), 10)
        answer = parse_answer(result["choices"][0]["message"]["content"], "t2i", 0)
        self.assertEqual(answer["wh_ratio"], "4:3")

    def test_discovers_uppercase_LLM_directory_and_registered_extra_paths(self):
        root = Path(self.temp.name)
        llm = root / "LLM"
        llm.mkdir()
        (llm / "Example.gguf").write_bytes(b"model")
        extra = root / "extra"
        extra.mkdir()
        (extra / "Other.gguf").write_bytes(b"model")
        folders = types.ModuleType("folder_paths")
        folders.models_dir = str(root)
        folders.folder_names_and_paths = {"LLM": ([str(llm), str(extra)], {".gguf"})}
        folders.get_folder_paths = lambda name: [str(llm), str(extra)]
        with patch.dict(sys.modules, {"folder_paths": folders}):
            self.assertEqual(local_models()["Example.gguf"], llm / "Example.gguf")
            self.assertEqual(local_models()["Other.gguf"], extra / "Other.gguf")


if __name__ == "__main__":
    unittest.main()

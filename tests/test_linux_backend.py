import copy
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import pe_runtime
from pe_runtime import LocalRuntime, local_models, parse_answer, vlm_module


class ProcessorList(list):
    def __call__(self, tokens, scores):
        for processor in self:
            scores = processor(tokens, scores)
        return scores


class Model:
    def __init__(self):
        self.closed = False
        self.calls = []
        self.thinking = []
        self.penalties = []
        self.response = '{"rewritten_prompt":"A blue dog.","wh_ratio":"1:1"}'

    def close(self):
        self.closed = True

    def reset(self):
        if self.closed:
            raise RuntimeError("Use after close")

    # Deliberately match the native API, without HTTP-only template kwargs.
    def create_chat_completion(self, messages, seed, stream, max_tokens,
                               temperature, top_p, top_k, min_p,
                               present_penalty, logits_processor):
        logits_processor([], [])
        self.calls.append(copy.deepcopy(messages))
        self.thinking.append((self.handler.enable_thinking,
                              self.handler.extra_template_arguments["enable_thinking"]))
        self.penalties.append(present_penalty)
        return {"choices": [{"message": {"content": self.response},
                             "finish_reason": "stop"}], "usage": {}}


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
        self.chat_handler = types.SimpleNamespace(
            enable_thinking=True, extra_template_arguments={"enable_thinking": True})
        self.llm = Model()
        self.llm.handler = self.chat_handler
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
        module.llama_cpp = types.SimpleNamespace(LogitsProcessorList=ProcessorList)
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

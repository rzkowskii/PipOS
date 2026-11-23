import asyncio
import sys
import types
import unittest

from mind import everyday
from mind.reply import Reply


def _fake_package(name, modules):
    package = types.ModuleType(name)
    package.__path__ = []
    sys.modules[name] = package
    for module_name, attrs in modules.items():
        module = types.ModuleType(f"{name}.{module_name}")
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[f"{name}.{module_name}"] = module
    return package


class Registry(unittest.TestCase):
    def setUp(self):
        self.calls = []

        async def first(text, ctx):
            self.calls.append("first")
            return None

        async def second(text, ctx):
            self.calls.append("second")
            return Reply("[neutral] second")

        def third(text, ctx):     # a plain function works too
            self.calls.append("third")
            return Reply("[neutral] third")

        async def broken(text, ctx):
            self.calls.append("broken")
            raise RuntimeError("boom")

        _fake_package("fake_everyday", {
            "first": {"handle": first, "may_match": lambda text: "one" in text},
            "second": {"handle": second, "may_match": lambda text: "two" in text},
            "third": {"handle": third},
            "broken": {"handle": broken, "may_match": lambda text: 1 / 0},
            "nohandler": {"may_match": lambda text: True},
        })

    def tearDown(self):
        for name in list(sys.modules):
            if name == "fake_everyday" or name.startswith("fake_everyday."):
                del sys.modules[name]

    def test_missing_modules_are_skipped_in_order(self):
        registry = everyday.Registry(("missing", "first", "gone", "second", "third"), package="fake_everyday")
        reply = asyncio.run(registry.handle("hello", None))
        self.assertEqual(reply.line, "[neutral] second")
        self.assertEqual(self.calls, ["first", "second"])
        self.assertEqual(registry.missing, ("missing", "gone"))

    def test_order_decides(self):
        registry = everyday.Registry(("third", "second"), package="fake_everyday")
        self.assertEqual(asyncio.run(registry.handle("x", None)).line, "[neutral] third")

    def test_broken_handler_is_passed_over(self):
        registry = everyday.Registry(("broken", "second"), package="fake_everyday")
        with self.assertLogs("being.everyday", level="WARNING"):
            reply = asyncio.run(registry.handle("x", None))
        self.assertEqual(reply.line, "[neutral] second")
        self.assertEqual(self.calls, ["broken", "second"])

    def test_module_without_handle_is_skipped(self):
        registry = everyday.Registry(("nohandler",), package="fake_everyday")
        self.assertIsNone(asyncio.run(registry.handle("x", None)))
        self.assertFalse(registry.may_match("anything"))
        self.assertEqual(registry.missing, ("nohandler",))

    def test_nothing_matches(self):
        registry = everyday.Registry(("first",), package="fake_everyday")
        self.assertIsNone(asyncio.run(registry.handle("x", None)))

    def test_may_match(self):
        registry = everyday.Registry(("broken", "first", "second", "third"), package="fake_everyday")
        with self.assertLogs("being.everyday", level="WARNING"):
            self.assertTrue(registry.may_match("two"))
            self.assertTrue(registry.may_match("one"))
            self.assertFalse(registry.may_match("three"))    # third has no may_match

    def test_imports_are_lazy_and_once(self):
        registry = everyday.Registry(("first",), package="fake_everyday")
        self.assertIsNone(registry._modules)
        registry.may_match("x")
        modules = registry._modules
        registry.may_match("y")
        self.assertIs(registry._modules, modules)

    def test_all_missing(self):
        registry = everyday.Registry(("nope", "neither"), package="fake_everyday")
        self.assertIsNone(asyncio.run(registry.handle("x", None)))
        self.assertFalse(registry.may_match("x"))


class RealOrder(unittest.TestCase):
    def test_order_is_the_spec(self):
        self.assertEqual(everyday.ORDER, ("readback", "calc", "units", "lists", "play", "body_talk", "device",
                                          "doctor", "action_log", "diary", "voice_engines"))

    def test_own_skills_load(self):
        names = {module.__name__.rsplit(".", 1)[-1] for module in everyday.Registry().modules()}
        self.assertTrue({"readback", "calc", "units", "lists", "play"} <= names)

    def test_gate(self):
        self.assertTrue(everyday.may_match("what's 17 times 23"))
        self.assertTrue(everyday.may_match("how many grams in a pound"))
        self.assertTrue(everyday.may_match("add milk to my shopping list"))
        self.assertTrue(everyday.may_match("tell me a joke"))
        self.assertTrue(everyday.may_match("say that again"))


if __name__ == "__main__":
    unittest.main()

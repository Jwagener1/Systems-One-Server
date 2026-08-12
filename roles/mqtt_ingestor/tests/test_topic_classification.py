import importlib.machinery
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(HERE, "..", "files", "app")
PIPELINE = os.path.join(HERE, "..", "files", "app", "ingestor", "pipeline.py")


def _install_db_stub() -> types.ModuleType:
    # pipeline.py does `from .db import DbWriter`. db.py itself (and its
    # write_broker_snapshot method) is added in a later consolidation task,
    # so a minimal stand-in is registered in sys.modules before loading --
    # this test only needs the module-level is_broker_topic() function,
    # never a real DbWriter instance.
    stub = types.ModuleType("ingestor.db")

    class DbWriter:
        pass

    stub.DbWriter = DbWriter
    sys.modules["ingestor.db"] = stub
    return stub


def _snapshot_ingestor_modules() -> dict:
    """
    Capture (and remove) every 'ingestor' / 'ingestor.*' entry currently in
    sys.modules. Sibling test files in this suite (test_spool.py,
    test_settings_writer.py) register their own incomplete
    sys.modules["ingestor.models"] stubs at module-import time and never
    clean them up, so under a combined run (`pytest roles/mqtt_ingestor/tests/`)
    those stale stubs -- lacking BrokerSnapshot -- would otherwise be reused
    here instead of the real models.py. Clearing the namespace first forces
    a fresh, correct import graph regardless of collection order.
    """
    stale = {
        k: v
        for k, v in sys.modules.items()
        if k == "ingestor" or k.startswith("ingestor.")
    }
    for k in stale:
        del sys.modules[k]
    return stale


def load():
    # pipeline.py uses package-relative imports (`from .config import
    # Config`, etc.) for its sibling modules, which are all real, already-
    # vendored files in this package (models.py, mqtt_client.py, metrics.py,
    # config.py, health.py, settings_writer.py, spool.py). Loading it under
    # a dotted, package-qualified name -- with the app dir on sys.path --
    # lets those relative imports resolve normally against the real files,
    # matching the pattern used in test_spool.py.
    saved = _snapshot_ingestor_modules()
    _install_db_stub()
    sys.path.insert(0, APP_DIR)
    try:
        loader = importlib.machinery.SourceFileLoader("ingestor.pipeline", PIPELINE)
        spec = importlib.util.spec_from_loader("ingestor.pipeline", loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ingestor.pipeline"] = mod
        loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(APP_DIR)
        # Undo everything this load() added to sys.modules (our db stub,
        # "ingestor" itself, and every real submodule pipeline.py pulled in
        # via relative import), then restore whatever was there before --
        # so this test's loading doesn't leak "ingestor.*" state into
        # sibling test files collected after it (e.g. Task 5's future
        # db.py tests).
        for k in list(sys.modules):
            if k == "ingestor" or k.startswith("ingestor."):
                del sys.modules[k]
        sys.modules.update(saved)


pipeline_mod = load()


class TopicClassification(unittest.TestCase):
    def test_sys_topics_are_broker_topics(self):
        self.assertTrue(pipeline_mod.is_broker_topic("$SYS/broker/clients/connected"))
        self.assertTrue(pipeline_mod.is_broker_topic("$SYS/broker/version"))

    def test_device_topics_are_not_broker_topics(self):
        self.assertFalse(pipeline_mod.is_broker_topic("systems-one/PEPKOR/JBH/DIM1/status"))
        self.assertFalse(pipeline_mod.is_broker_topic("systemsone/PEPKOR/JBH/DIM1/OS/cpu"))

    def test_empty_topic_is_not_a_broker_topic(self):
        self.assertFalse(pipeline_mod.is_broker_topic(""))


if __name__ == "__main__":
    unittest.main()

import importlib.machinery
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "..", "files", "app", "ingestor", "config.py")


def load():
    loader = importlib.machinery.SourceFileLoader("mi_config", CONFIG)
    spec = importlib.util.spec_from_loader("mi_config", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mi_config"] = mod
    loader.exec_module(mod)
    return mod


cfg_mod = load()

REQUIRED_ENV = {
    "MQTT_HOST": "mosquitto",
    "MQTT_TOPICS": "systems-one/#,systemsone/#,$SYS/#",
    "DB_HOST": "mssql",
    "DB_NAME": "S1_Remote_Monitoring",
    "DB_USER": "admin",
    "DB_PASSWORD": "x",
}


class EnvSandbox(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in REQUIRED_ENV}
        os.environ.update(REQUIRED_ENV)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class MultiTopicParsing(EnvSandbox):
    def test_comma_separated_topics_parsed_into_tuple(self):
        cfg = cfg_mod.load_config()
        self.assertEqual(
            cfg.mqtt.topics,
            ("systems-one/#", "systemsone/#", "$SYS/#"),
        )

    def test_whitespace_around_topics_stripped(self):
        os.environ["MQTT_TOPICS"] = " systems-one/# , systemsone/# "
        cfg = cfg_mod.load_config()
        self.assertEqual(cfg.mqtt.topics, ("systems-one/#", "systemsone/#"))

    def test_empty_segments_dropped(self):
        os.environ["MQTT_TOPICS"] = "systems-one/#,,systemsone/#"
        cfg = cfg_mod.load_config()
        self.assertEqual(cfg.mqtt.topics, ("systems-one/#", "systemsone/#"))

    def test_missing_mqtt_topics_raises(self):
        os.environ.pop("MQTT_TOPICS")
        with self.assertRaises(RuntimeError):
            cfg_mod.load_config()


if __name__ == "__main__":
    unittest.main()

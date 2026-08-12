"""Unit tests for mqtt_ingestor device validation (validation.py)."""
import importlib.machinery
import importlib.util
import logging
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
VALIDATION = os.path.join(HERE, "..", "files", "app", "ingestor", "validation.py")


def load():
    loader = importlib.machinery.SourceFileLoader("mi_validation", VALIDATION)
    spec = importlib.util.spec_from_loader("mi_validation", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


val = load()
log = logging.getLogger("test")


class EnvSandbox(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for k in ("INGEST_ALLOWED_CUSTOMERS", "INGEST_ALLOWED_LOCATIONS"):
            self._saved[k] = os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class SerialChecks(EnvSandbox):
    def test_machine_name_as_serial_rejected(self):
        for serial in ("DIM5", "dim12", "STATIC1"):
            self.assertFalse(val.validate_device(serial, "PEPKOR", "JBH", "DIM5", log), serial)

    def test_short_or_malformed_serial_rejected(self):
        for serial in ("abc", "a!b#c", ""):
            self.assertFalse(val.validate_device(serial, "PEPKOR", "JBH", "DIM5", log), serial)

    def test_real_serials_accepted(self):
        for serial in ("018370-01-2", "019000-01-1", "018389-02-01"):
            self.assertTrue(val.validate_device(serial, "PEPKOR", "JBH", "DIM1", log), serial)


class AllowAllByDefault(EnvSandbox):
    def test_new_customers_accepted_when_env_unset(self):
        cases = [
            ("018370-01-2", "DCB", "DUR"),
            ("019000-01-1", "MADIBANA", "JHB"),
            ("017000-01-1", "SNOWSOFT", "JHB"),
            ("018389-02-01", "BEX", "CPT"),
            ("018377-01-1", "PEP AFRICA", "DUR"),
        ]
        for serial, customer, location in cases:
            self.assertTrue(
                val.validate_device(serial, customer, location, "DIM1", log),
                f"{customer}@{location} must be accepted with no allowlist configured",
            )

    def test_empty_env_means_allow_all(self):
        os.environ["INGEST_ALLOWED_CUSTOMERS"] = ""
        os.environ["INGEST_ALLOWED_LOCATIONS"] = "  "
        self.assertTrue(val.validate_device("018370-01-2", "ANYONE", "ANYWHERE", "DIM1", log))


class ConfiguredAllowlists(EnvSandbox):
    def test_customer_allowlist_enforced(self):
        os.environ["INGEST_ALLOWED_CUSTOMERS"] = "PEPKOR, PEP"
        self.assertTrue(val.validate_device("018370-01-2", "PEPKOR", "JBH", "DIM1", log))
        self.assertFalse(val.validate_device("018370-01-2", "DCB", "DUR", "DIM1", log))

    def test_location_allowlist_enforced(self):
        os.environ["INGEST_ALLOWED_LOCATIONS"] = "JBH,JHB,HDH"
        self.assertTrue(val.validate_device("019000-01-1", "MADIBANA", "JHB", "DIM1", log))
        self.assertFalse(val.validate_device("019000-01-1", "MADIBANA", "PE", "DIM1", log))

    def test_case_and_whitespace_insensitive(self):
        os.environ["INGEST_ALLOWED_CUSTOMERS"] = " pepkor ,dcb "
        self.assertTrue(val.validate_device("018370-01-2", "DCB", "DUR", "DIM1", log))
        self.assertTrue(val.validate_device("018370-01-2", "Pepkor", "DUR", "DIM1", log))
        self.assertFalse(val.validate_device("018370-01-2", "MADIBANA", "DUR", "DIM1", log))


if __name__ == "__main__":
    unittest.main()

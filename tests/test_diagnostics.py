import unittest
import common
class DiagnosticPrivacy(unittest.TestCase):
    def test_authentication_category(self):
        value=common._backend_failure_hint(b"host.invalid: Permission denied (publickey)")
        self.assertEqual(value,"AUTHENTICATION")
        self.assertNotIn("host.invalid",value)
    def test_unknown_output_is_not_echoed(self):
        value=common._backend_failure_hint(b"DO_NOT_EXPOSE_PRIVATE_DATA")
        self.assertEqual(value,"BACKEND_EXIT_OR_IO")
        self.assertNotIn("PRIVATE",value)

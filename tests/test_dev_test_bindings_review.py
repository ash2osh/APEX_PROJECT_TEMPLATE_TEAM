"""Controller regressions for binding boundaries and Oracle literal syntax."""
import unittest

from scripts.dev_test_bindings import DevTestBindingError, RenderContext, render_object_tokens, validate_bindings


class BindingReviewTests(unittest.TestCase):
    def test_q_quote_ends_without_bracket_balancing(self):
        context = RenderContext('APP', 'run', {'items': 'DT_0123456789AB_ITEMS'}, {})
        self.assertEqual(
            render_object_tokens("SELECT q'[[x]' AS payload FROM ${object.items}", context),
            'SELECT q\'[[x]\' AS payload FROM "APP"."DT_0123456789AB_ITEMS"',
        )

    def test_normalized_alias_cannot_silently_overwrite_binding(self):
        with self.assertRaises(DevTestBindingError):
            validate_bindings({'DEVTEST_ID': 1, ':DEVTEST_ID': 2})

    def test_injected_run_id_respects_value_limit(self):
        with self.assertRaises(DevTestBindingError):
            RenderContext('APP', 'x' * 4001, {}, {})

    def test_rejected_value_not_echoed(self):
        secret = {'token': 'PRIVATE_FIXTURE_VALUE'}
        with self.assertRaises(DevTestBindingError) as caught:
            validate_bindings({'DEVTEST_ID': secret})
        self.assertNotIn('PRIVATE_FIXTURE_VALUE', str(caught.exception))

    def test_unpaired_surrogate_is_structured_refusal(self):
        with self.assertRaises(DevTestBindingError):
            validate_bindings({'DEVTEST_ID': '\ud800'})

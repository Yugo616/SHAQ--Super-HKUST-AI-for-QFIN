import unittest
from shaq_daily_oracle.history_methods import equivalent_method_documents
from shaq_daily_oracle.module_rules import default_rule


class HistoryMethodTests(unittest.TestCase):
    def test_default_module_packaging_does_not_split_method(self):
        old = {'skills/market/SKILL.md': 'method', 'decision/decision.js': 'rule'}
        new = dict(old, **{'modules/market/compute.js': default_rule('market'),
                          'modules/market/cases.json': '{}'})
        self.assertTrue(equivalent_method_documents(old, new))
        new['modules/market/compute.js'] = 'function compute(x){return {signal:1}}'
        self.assertFalse(equivalent_method_documents(old, new))

    def test_changed_method_and_empty_documents_never_alias(self):
        self.assertFalse(equivalent_method_documents({}, {}))
        self.assertFalse(equivalent_method_documents({'SKILL.md':'a'}, {'SKILL.md':'b'}))

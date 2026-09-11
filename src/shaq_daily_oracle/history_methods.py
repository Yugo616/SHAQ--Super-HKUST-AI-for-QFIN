"""Display aliases derived from verified method content, not author names."""
from .module_rules import MODULES, default_rule


def equivalent_method_documents(left, right):
    def normalize(documents):
        result = dict(documents)
        for module in MODULES:
            code = f'modules/{module}/compute.js'
            if result.get(code, '').strip() == default_rule(module):
                result.pop(code)
                result.pop(f'modules/{module}/cases.json', None)
        return result
    return bool(left and right) and normalize(left) == normalize(right)

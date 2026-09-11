"""Display aliases derived from verified method content, not author names."""
from .module_rules import MODULES, default_rule
from .hashing import sha256_payload


def _normalized_method_documents(documents):
    result = dict(documents)
    for module in MODULES:
        code = f'modules/{module}/compute.js'
        if result.get(code, '').strip() == default_rule(module):
            result.pop(code)
            result.pop(f'modules/{module}/cases.json', None)
    return result


def method_document_identity(documents):
    return sha256_payload(_normalized_method_documents(documents)) if documents else ''


def equivalent_method_documents(left, right):
    return bool(left and right) and _normalized_method_documents(left) == _normalized_method_documents(right)

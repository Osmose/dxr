import ast
from contextlib import contextmanager


def package_for_module(module_path):
    return module_path.rsplit('.', 1)[0] if '.' in module_path else None


def convert_node_to_name(node):
    """Convert an AST node to a name if possible. Return None if we
    can't (such as function calls).

    """
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        value_name = convert_node_to_name(node.value)
        if value_name:
            return value_name + '.' + node.attr
    else:
        return None


def relative_module_path(python_path, module_path):
    """Covert an absolute path to a module to a path relative to the
    given python path.

    """
    return module_path.replace(python_path, '', 1).strip('/')


def path_to_module(python_path, module_path):
    """Convert a file path into a dotted module path, using the given
    python_path as the base directory that modules live in.

    """
    module_path = relative_module_path(python_path, module_path)
    module_path = trim_end(module_path, '.py')
    module_path = trim_end(module_path, '/__init__')
    return module_path.replace('/', '.')


def trim_end(string, end):
    if string.endswith(end):
        return string[:-len(end)]
    else:
        return string


class ClassFunctionVisitorMixin(object):
    """Mixin for NodeVisitors that detects member functions on classes
    and handles them specifically.

    """
    def __init__(self, *args, **kwargs):
        super(ClassFunctionVisitorMixin, self).__init__(*args, **kwargs)

        self._current_class = None
        self._visiting_class_functions = False

    def visit_ClassDef(self, node):
        old_class = self._current_class
        self._current_class = node
        with self._visit_class_functions(True):
            self.generic_visit(node)
        self._current_class = old_class

    def visit_FunctionDef(self, node):
        if self._visiting_class_functions:
            self.visit_ClassFunction(self._current_class, node)

        # Disable collection in case there are any inner functions.
        with self._visit_class_functions(False):
            self.generic_visit(node)

    def visit_ClassFunction(self, class_node, function_node):
        raise NotImplementedError()

    @contextmanager
    def _visit_class_functions(self, visiting):
        old = self._visiting_class_functions
        self._visiting_class_functions = visiting
        yield
        self._visiting_class_functions = old

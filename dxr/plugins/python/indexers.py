import ast
import token
import tokenize
from StringIO import StringIO

from dxr.build import unignored
from dxr.filters import LINE
from dxr.indexers import (Extent, FileToIndex as FileToIndexBase,
                          iterable_per_line, Position, split_into_lines,
                          TreeToIndex as TreeToIndexBase,
                          QUALIFIED_NEEDLE, with_start_and_end)
from dxr.menus import definition_menu
from dxr.plugins.python.analysis import TreeAnalysis
from dxr.plugins.python.menus import class_menu
from dxr.plugins.python.utils import (ClassFunctionVisitor,
                                      convert_node_to_name, path_to_module,
                                      QualNameVisitor)


mappings = {
    LINE: {
        'properties': {
            'py_type': QUALIFIED_NEEDLE,
            'py_function': QUALIFIED_NEEDLE,
            'py_derived': QUALIFIED_NEEDLE,
            'py_bases': QUALIFIED_NEEDLE,
            'py_callers': QUALIFIED_NEEDLE,
            'py_called_by': QUALIFIED_NEEDLE,
            'py_overrides': QUALIFIED_NEEDLE,
            'py_overridden': QUALIFIED_NEEDLE,
        },
    },
}


class _FileToIgnore(object):
    """A file that we don't want to bother indexing, usually due to
    syntax errors.

    """
    def is_interesting(self):
        return False
FILE_TO_IGNORE = _FileToIgnore()


class TreeToIndex(TreeToIndexBase):
    @property
    def unignored_files(self):
        return unignored(self.tree.source_folder, self.tree.ignore_paths,
                         self.tree.ignore_filenames)

    def post_build(self):
        paths = ((path, self.tree.source_encoding) for path in self.unignored_files
                 if is_interesting(path))
        self.tree_analysis = TreeAnalysis(
            python_path=self.plugin_config.python_path,
            source_folder=self.tree.source_folder,
            paths=paths)

    def file_to_index(self, path, contents):
        if path in self.tree_analysis.ignore_paths:
            return FILE_TO_IGNORE
        else:
            return FileToIndex(path, contents, self.plugin_name, self.tree,
                               tree_analysis=self.tree_analysis)


class IndexingNodeVisitor(ClassFunctionVisitor, QualNameVisitor):
    """NodeVisitor that walks through the nodes in an abstract syntax
    tree and finds interesting things to index.

    """

    def __init__(self, file_to_index, tree_analysis):
        self.module_path = file_to_index.module_path  # For QualNameVisitor
        super(IndexingNodeVisitor, self).__init__()

        self.file_to_index = file_to_index
        self.tree_analysis = tree_analysis
        self.function_call_stack = []  # List of lists of function names.
        self.needles = []
        self.refs = []

    def visit_FunctionDef(self, node):
        # Index the function itself for the function: filter.
        start, end = self.file_to_index.get_node_start_end(node)
        self.yield_needle('py_function', node.name, start, end)

        # Index function calls within this function for the callers: and
        # called-by filters.
        self.function_call_stack.append([])
        super(IndexingNodeVisitor, self).visit_FunctionDef(node)
        call_needles = self.function_call_stack.pop()
        for name, call_start, call_end in call_needles:
            self.yield_needle('py_callers', name, start, end)
            self.yield_needle('py_called_by', node.name, call_start, call_end)

    def visit_Call(self, node):
        start, end = self.file_to_index.get_node_start_end(node)
        function_name = convert_node_to_name(node.func)

        if function_name:
            # Save this call if we're currently tracking function calls.
            if self.function_call_stack:
                call_needles = self.function_call_stack[-1]
                call_needles.append((function_name, start, end))

            # Show menu for jumping to the definition of this function.
            qualname = self.module_path + '.' + function_name
            function_def = self.tree_analysis.get_definition(qualname)
            if function_def:
                menu = definition_menu(self.file_to_index.tree,
                                       function_def.path, function_def.line)
                self.yield_ref(start, end, menu)

        self.generic_visit(node)

    def visit_ClassDef(self, node):
        super(IndexingNodeVisitor, self).visit_ClassDef(node)

        # Index the class itself for the type: filter.
        start, end = self.file_to_index.get_node_start_end(node)
        self.yield_needle('py_type', node.name, start, end)

        # Index the class hierarchy for classes for the derived: and
        # bases: filters.
        bases = self.tree_analysis.get_base_classes(node.qualname)
        for qualname in bases:
            name = qualname.split('.')[-1]
            self.yield_needle(needle_type='py_derived',
                              name=name, qualname=qualname,
                              start=start, end=end)

        derived_classes = self.tree_analysis.get_derived_classes(node.qualname)
        for qualname in derived_classes:
            name = qualname.split('.')[-1]
            self.yield_needle(needle_type='py_bases',
                              name=name, qualname=qualname,
                              start=start, end=end)

        # Show a menu when hovering over this class.
        self.yield_ref(start, end,
                       class_menu(self.file_to_index.tree, node.qualname))


    def visit_ClassFunction(self, class_node, function_node):
        function_qualname = class_node.qualname + '.' + function_node.name
        start, end = self.file_to_index.get_node_start_end(function_node)

        # Index this function as being overridden by other functions for
        # the overridden: filter.
        for qualname in self.tree_analysis.overridden_functions[function_qualname]:
            name = qualname.rsplit('.')[-1]
            self.yield_needle(needle_type='py_overridden',
                              name=name, qualname=qualname,
                              start=start, end=end)

        # Index this function as overriding other functions for the
        # overrides: filter.
        for qualname in self.tree_analysis.overriding_functions[function_qualname]:
            name = qualname.rsplit('.')[-1]
            self.yield_needle(needle_type='py_overrides',
                              name=name, qualname=qualname,
                              start=start, end=end)

    def yield_needle(self, *args, **kwargs):
        needle = line_needle(*args, **kwargs)
        self.needles.append(needle)

    def yield_ref(self, start, end, menu):
        self.refs.append((
            self.file_to_index.char_offset(*start),
            self.file_to_index.char_offset(*end),
            (menu, None),
        ))


class FileToIndex(FileToIndexBase):
    def __init__(self, path, contents, plugin_name, tree, tree_analysis):
        """
        :arg tree_analysis: TreeAnalysisResult object with the results
        from the post-build analysis.

        """
        super(FileToIndex, self).__init__(path, contents, plugin_name, tree)

        self.tree_analysis = tree_analysis
        self.module_path = path_to_module(tree_analysis.python_path, self.path)

        self._visitor = None

    def is_interesting(self):
        return is_interesting(self.path)

    @property
    def visitor(self):
        """Return IndexingNodeVisitor for this file, lazily creating and
        running it if it doesn't exist yet.

        """
        if not self._visitor:
            self.node_start_table = self.analyze_tokens()
            self._visitor = IndexingNodeVisitor(self, self.tree_analysis)
            syntax_tree = ast.parse(self.contents)
            self._visitor.visit(syntax_tree)
        return self._visitor

    def needles_by_line(self):
        return iterable_per_line(
            with_start_and_end(
                split_into_lines(
                    self.visitor.needles
                )
            )
        )

    def refs(self):
        return self.visitor.refs

    def analyze_tokens(self):
        """Split the file into tokens and analyze them for data needed
        for indexing.

        """
        # AST nodes for classes and functions point to the position of
        # their 'def' and 'class' tokens. To get the position of their
        # names, we look for 'def' and 'class' tokens and store the
        # position of the token immediately following them.
        node_start_table = {}
        previous_start = None
        token_gen = tokenize.generate_tokens(StringIO(self.contents).readline)

        for tok_type, tok_name, start, end, _ in token_gen:
            if tok_type != token.NAME:
                continue

            if tok_name in ('def', 'class'):
                previous_start = start
            elif previous_start is not None:
                node_start_table[previous_start] = start
                previous_start = None

        return node_start_table

    def get_node_start_end(self, node):
        """Return start and end positions within the file for the given
        AST Node.

        """
        start = node.lineno, node.col_offset
        if start in self.node_start_table:
            start = self.node_start_table[start]

        end = None
        if isinstance(node, ast.ClassDef) or isinstance(node, ast.FunctionDef):
            end = start[0], start[1] + len(node.name)
        elif isinstance(node, ast.Call):
            name = convert_node_to_name(node.func)
            if name:
                end = start[0], start[1] + len(name)

        return start, end


def line_needle(needle_type, name, start, end, qualname=None):
    data = {
        'name': name,
        'start': start[1],
        'end': end[1]
    }

    if qualname:
        data['qualname'] = qualname

    return (
        needle_type,
        data,
        Extent(Position(row=start[0],
                        col=start[1]),
               Position(row=end[0],
                        col=end[1]))
    )


def is_interesting(path):
    """Determine if the file at the given path is interesting enough to
    analyze.

    """
    return path.endswith('.py')

from typing import Dict, Union, List, Tuple, Callable, Optional, Any
import typing
import sys
import random
import copy
import bisect

from pycparser import c_ast as ca, c_parser, c_generator

from ast_types import (SimpleType, build_typemap, decayed_expr_type, same_type,
        set_decl_name)

# Set to true to perform expression type detection eagerly. This can help when
# debugging crashes in the ast_types code.
DEBUG_EAGER_TYPES = False

Indices = Dict[ca.Node, int]
Block = Union[ca.Compound, ca.Case, ca.Default]

class PatchedCGenerator(c_generator.CGenerator):
    """Like a CGenerator, except it keeps else if's prettier despite
    the terrible things we've done to them in normalize_ast."""
    def visit_If(self, n: ca.If) -> None:
        n2 = n
        if (n.iffalse and isinstance(n.iffalse, ca.Compound) and
                n.iffalse.block_items and
                len(n.iffalse.block_items) == 1 and
                isinstance(n.iffalse.block_items[0], ca.If)):
            n2 = ca.If(cond=n.cond, iftrue=n.iftrue,
                    iffalse=n.iffalse.block_items[0])
        super().visit_If(n2)

def to_c(node: ca.Node) -> str:
    source = PatchedCGenerator().visit(node)
    if '#pragma' not in source:
        return source
    lines = source.split('\n')
    out = []
    same_line = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('#pragma'):
            if stripped == '#pragma sameline start':
                same_line += 1
                continue
            elif stripped == '#pragma sameline end':
                same_line -= 1
                if same_line == 0:
                    out.append('\n')
                continue
        if not same_line:
            line += '\n'
        out.append(line)
    assert same_line == 0
    return ''.join(out).rstrip() + '\n'

def find_fns(ast: ca.FileAST) -> List[ca.FuncDef]:
    ret = []
    for node in ast.ext:
        if isinstance(node, ca.FuncDef):
            ret.append(node)
    return ret

def compute_node_indices(top_node: ca.Node) -> Indices:
    indices = {}
    cur_index = 0
    class Visitor(ca.NodeVisitor):
        def generic_visit(self, node: ca.Node) -> None:
            nonlocal cur_index
            indices[node] = cur_index
            cur_index += 1
            super().generic_visit(node)
    Visitor().visit(top_node)
    return indices

def compute_write_locations(
    top_node: ca.Node, indices: Indices
) -> Dict[str, List[int]]:
    writes : Dict[str, List[int]] = {}
    def add_write(var_name: str, loc: int) -> None:
        if var_name not in writes:
            writes[var_name] = []
        else:
            assert loc > writes[var_name][-1], \
                    "consistent traversal order should guarantee monotonicity here"
        writes[var_name].append(loc)
    class Visitor(ca.NodeVisitor):
        def visit_Decl(self, node: ca.Decl) -> None:
            if node.name:
                add_write(node.name, indices[node])
            self.generic_visit(node)
        def visit_UnaryOp(self, node: ca.UnaryOp) -> None:
            if node.op in ['p++', 'p--', '++', '--'] and isinstance(node.expr, ca.ID):
                add_write(node.expr.name, indices[node])
            self.generic_visit(node)
        def visit_Assignment(self, node: ca.Assignment) -> None:
            if isinstance(node.lvalue, ca.ID):
                add_write(node.lvalue.name, indices[node])
            self.generic_visit(node)
    Visitor().visit(top_node)
    return writes

def compute_read_locations(
    top_node: ca.Node, indices: Indices
) -> Dict[str, List[int]]:
    reads: Dict[str, List[int]] = {}
    for node in find_var_reads(top_node):
        var_name = node.name
        loc = indices[node]
        if var_name not in reads:
            reads[var_name] = []
        else:
            assert loc > reads[var_name][-1], \
                    "consistent traversal order should guarantee monotonicity here"
        reads[var_name].append(loc)
    return reads

def find_var_reads(top_node: ca.Node) -> List[ca.ID]:
    ret = []
    class Visitor(ca.NodeVisitor):
        def visit_Decl(self, node: ca.Decl) -> None:
            if node.init:
                self.visit(node.init)
        def visit_ID(self, node: ca.ID) -> None:
            ret.append(node)
        def visit_StructRef(self, node: ca.StructRef) -> None:
            self.visit(node.name)
        def visit_Assignment(self, node: ca.Assignment) -> None:
            if isinstance(node.lvalue, ca.ID):
                return
            self.generic_visit(node)
    Visitor().visit(top_node)
    return ret

def replace_subexprs(
    top_node: ca.Node,
    callback: Callable[[ca.Expression], Any]
) -> None:
    def rec(orig_node: ca.Node, toplevel: bool=False) -> Any:
        node: ca.AnyNode = typing.cast(ca.AnyNode, orig_node)
        if isinstance(node, ca.Assignment):
            node.rvalue = rec(node.rvalue)
        elif isinstance(node, ca.StructRef):
            node.name = rec(node.name)
        elif isinstance(node, (ca.Return, ca.Cast)):
            if node.expr:
                node.expr = rec(node.expr)
        elif isinstance(node, (ca.Constant, ca.ID)):
            if not toplevel:
                x = callback(node)
                if x: return x
        elif isinstance(node, ca.UnaryOp):
            if not toplevel:
                x = callback(node)
                if x: return x
            if node.op not in ['p++', 'p--', '++', '--', '&']:
                node.expr = rec(node.expr)
        elif isinstance(node, ca.BinaryOp):
            if not toplevel:
                x = callback(node)
                if x: return x
            node.left = rec(node.left)
            node.right = rec(node.right)
        elif isinstance(node, ca.Compound):
            for sub in node.block_items or []:
                rec(sub, True)
        elif isinstance(node, (ca.Case, ca.Default)):
            for sub in node.stmts or []:
                rec(sub, True)
        elif isinstance(node, ca.FuncCall):
            if not toplevel:
                x = callback(node)
                if x: return x
            if node.args:
                rec(node.args, True)
        elif isinstance(node, ca.ExprList):
            if not toplevel:
                x = callback(node)
                if x: return x
            for i in range(len(node.exprs)):
                node.exprs[i] = rec(node.exprs[i])
        elif isinstance(node, ca.ArrayRef):
            if not toplevel:
                x = callback(node)
                if x: return x
            node.name = rec(node.name)
            node.subscript = rec(node.subscript)
        elif isinstance(node, ca.Decl):
            if node.init:
                node.init = rec(node.init)
        elif isinstance(node, ca.For):
            if node.init:
                node.init = rec(node.init)
            if node.cond:
                node.cond = rec(node.cond)
            if node.next:
                node.next = rec(node.next, True)
            node.stmt = rec(node.stmt, True)
        elif isinstance(node, ca.TernaryOp):
            if not toplevel:
                x = callback(node)
                if x: return x
            node.cond = rec(node.cond)
            node.iftrue = rec(node.iftrue)
            node.iffalse = rec(node.iffalse)
        elif isinstance(node, ca.While):
            node.cond = rec(node.cond)
            node.stmt = rec(node.stmt, True)
        elif isinstance(node, ca.DoWhile):
            node.stmt = rec(node.stmt, True)
            node.cond = rec(node.cond)
        elif isinstance(node, ca.Switch):
            node.cond = rec(node.cond)
            node.stmt = rec(node.stmt, True)
        elif isinstance(node, ca.Label):
            node.stmt = rec(node.stmt, True)
        elif isinstance(node, ca.If):
            node.cond = rec(node.cond)
            node.iftrue = rec(node.iftrue, True)
            if node.iffalse:
                node.iffalse = rec(node.iffalse, True)
        elif isinstance(node, (ca.TypeDecl, ca.PtrDecl, ca.ArrayDecl,
                ca.Typename, ca.IdentifierType, ca.Struct,
                ca.Union, ca.Enum, ca.EmptyStatement, ca.Pragma,
                ca.Break, ca.Continue, ca.Goto, ca.CompoundLiteral,
                ca.Typedef, ca.FuncDecl, ca.FuncDef,
                ca.EllipsisParam, ca.Enumerator, ca.EnumeratorList,
                ca.FileAST, ca.InitList, ca.NamedInitializer,
                ca.ParamList)):
            pass
        else:
            _: None = node
            assert False, f"Node with unknown type: {node}"
        return node

    rec(top_node, True)

def get_block_stmts(block: Block, force: bool) -> List[ca.Statement]:
    if isinstance(block, ca.Compound):
        ret = block.block_items or []
        if force and not block.block_items:
            block.block_items = ret
    else:
        ret = block.stmts or []
        if force and not block.stmts:
            block.stmts = ret
    return ret

def insert_decl(fn: ca.FuncDef, decl: ca.Decl) -> None:
    assert fn.body.block_items, "Non-empty function"
    for index, stmt in enumerate(fn.body.block_items):
        if not isinstance(stmt, ca.Decl):
            break
    else:
        index = len(fn.body.block_items)
    fn.body.block_items[index:index] = [decl]

def insert_statement(block: Block, index: int, stmt: ca.Statement) -> None:
    stmts = get_block_stmts(block, True)
    stmts[index:index] = [stmt]

def brace_nested_blocks(stmt: ca.Statement) -> None:
    def brace(stmt: ca.Statement) -> Block:
        if isinstance(stmt, (ca.Compound, ca.Case, ca.Default)):
            return stmt
        return ca.Compound([stmt])
    if isinstance(stmt, (ca.For, ca.While, ca.DoWhile)):
        stmt.stmt = brace(stmt.stmt)
    elif isinstance(stmt, ca.If):
        stmt.iftrue = brace(stmt.iftrue)
        if stmt.iffalse:
            stmt.iffalse = brace(stmt.iffalse)
    elif isinstance(stmt, ca.Switch):
        stmt.stmt = brace(stmt.stmt)
    elif isinstance(stmt, ca.Label):
        brace_nested_blocks(stmt.stmt)

def for_nested_blocks(
    stmt: ca.Statement,
    callback: Callable[[Block], None]
) -> None:
    def invoke(stmt: ca.Statement) -> None:
        assert isinstance(stmt, (ca.Compound, ca.Case, ca.Default)), \
                "brace_nested_blocks should have turned nested statements into blocks"
        callback(stmt)
    if isinstance(stmt, ca.Compound):
        invoke(stmt)
    elif isinstance(stmt, (ca.For, ca.While, ca.DoWhile)):
        invoke(stmt.stmt)
    elif isinstance(stmt, ca.If):
        if stmt.iftrue:
            invoke(stmt.iftrue)
        if stmt.iffalse:
            invoke(stmt.iffalse)
    elif isinstance(stmt, ca.Switch):
        invoke(stmt.stmt)
    elif isinstance(stmt, (ca.Case, ca.Default)):
        invoke(stmt)
    elif isinstance(stmt, ca.Label):
        for_nested_blocks(stmt.stmt, callback)

def perm_temp_for_expr(fn: ca.FuncDef, ast: ca.FileAST) -> None:
    Place = Tuple[Block, int, ca.Statement]
    einds: Dict[int, int] = {}
    sumprob: float = 0
    targetprob: Optional[float] = None
    found: Optional[Tuple[Place, ca.Expression, ca.ID, SimpleType, bool]] = None
    indices = compute_node_indices(fn)
    writes = compute_write_locations(fn, indices)
    reads = compute_read_locations(fn, indices)
    typemap = build_typemap(ast)

    def rec(block: Block, reuse_cands: List[str]) -> None:
        stmts = get_block_stmts(block, False)
        reuse_cands = reuse_cands[:]
        assignment_cands: List[Place] = [] # places to insert before
        past_decls = False
        for index, stmt in enumerate(stmts):
            if isinstance(stmt, ca.Decl):
                assert stmt.name, "Anonymous declarations cannot happen in functions"
                if not isinstance(stmt.type, ca.ArrayDecl):
                    reuse_cands.append(stmt.name)
                    if not isinstance(stmt.type, ca.PtrDecl):
                        # Make non-pointers more common
                        reuse_cands.append(stmt.name)
            else:
                past_decls = True
            if past_decls:
                assignment_cands.append((block, index, stmt))

            for_nested_blocks(stmt, lambda b: rec(b, reuse_cands))

            def replacer(expr: ca.Expression) -> Optional[ca.Expression]:
                nonlocal sumprob
                nonlocal found
                if found is not None:
                    return None

                if DEBUG_EAGER_TYPES:
                    decayed_expr_type(expr, typemap)

                eind = einds.get(id(expr), 0)
                sub_reads = find_var_reads(expr)
                latest_write = -1
                for sub_read in sub_reads:
                    var_name = sub_read.name
                    if var_name not in writes:
                        continue
                    # Find the first write that is strictly before indices[expr]
                    ind = bisect.bisect_left(writes[var_name], indices[expr])
                    if ind == 0:
                        continue
                    latest_write = max(latest_write, writes[var_name][ind - 1])

                for place in assignment_cands[::-1]:
                    # If expr contains an ID which is written to within
                    # [place, expr), bail out; we're trying to move the
                    # assignment too high up.
                    # TODO: also fail on moving past function calls, or
                    # possibly-aliasing writes.
                    if indices[place[2]] <= latest_write:
                        break

                    prob = 1 / (1 + eind)
                    if isinstance(expr, (ca.ID, ca.Constant)):
                        prob *= 0.5
                    sumprob += prob
                    if targetprob is not None and sumprob > targetprob:
                        type: SimpleType = decayed_expr_type(expr, typemap)
                        reused = False
                        if random.randint(0,1) and reuse_cands:
                            var = ca.ID(random.choice(reuse_cands))
                            var_type: SimpleType = decayed_expr_type(var, typemap)
                            if same_type(var_type, type, typemap, allow_similar=True):
                                reused = True
                        if not reused:
                            var = ca.ID('new_var')
                        found = (place, expr, var, type, reused)
                        return var
                    eind += 1
                einds[id(expr)] = eind
                return None
            replace_subexprs(stmt, replacer)

    rec(fn.body, [])
    targetprob = random.uniform(0, sumprob)
    sumprob = 0
    einds = {}
    rec(fn.body, [])

    assert found is not None
    location, expr, var, type, reused = found
    # print("replacing:", to_c(expr))
    block, index, _ = location
    assignment = ca.Assignment('=', var, expr)
    insert_statement(block, index, assignment)
    if not reused:
        decl = ca.Decl(name=var.name, quals=[], storage=[], funcspec=[],
                type=copy.deepcopy(type), init=None, bitsize=None)
        set_decl_name(decl)
        insert_decl(fn, decl)

def perm_sameline(fn: ca.FuncDef, ast: ca.FileAST) -> None:
    cands: List[Tuple[Block, int]] = []
    def rec(block: Block) -> None:
        stmts = get_block_stmts(block, False)
        for index, stmt in enumerate(stmts):
            cands.append((block, index))
            for_nested_blocks(stmt, rec)
        cands.append((block, len(stmts)))
    rec(fn.body)
    n = len(cands)
    assert n >= 3
    # Generate a small random interval
    lef: float = n - 2
    for i in range(4):
        lef *= random.uniform(0, 1)
    le = int(lef) + 2
    i = random.randrange(n - le)
    j = i + le
    # Insert the second statement first, since inserting a statement may cause
    # later indices to move.
    insert_statement(cands[j][0], cands[j][1], ca.Pragma("sameline end"))
    insert_statement(cands[i][0], cands[i][1], ca.Pragma("sameline start"))

def normalize_ast(ast: ca.FileAST) -> None:
    # Add braces to all ifs/fors/etc., to make it easier to insert statements.
    fn = find_fns(ast)[0]
    def rec(block: Block) -> None:
        stmts = get_block_stmts(block, False)
        for stmt in stmts:
            brace_nested_blocks(stmt)
            for_nested_blocks(stmt, rec)
    rec(fn.body)

class Randomizer:
    def __init__(self, start_ast: ca.FileAST) -> None:
        self.start_ast = start_ast
        normalize_ast(self.start_ast)
        self.ast = self.start_ast

    def get_current_source(self) -> str:
        return to_c(self.ast)

    def randomize(self) -> None:
        ast = copy.deepcopy(self.start_ast)
        fn = find_fns(ast)[0]
        methods = [
            (perm_temp_for_expr, 90),
            #(perm_sameline, 10),
        ]
        method = random.choice([x for (elem, prob) in methods for x in [elem]*prob])
        method(fn, ast)
        self.ast = ast
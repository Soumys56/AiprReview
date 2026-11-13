import hashlib
from pathlib import PurePosixPath
import tree_sitter
import tree_sitter_python as tspython
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts

PY_LANGUAGE = tree_sitter.Language(tspython.language())
JS_LANGUAGE = tree_sitter.Language(tsjs.language())
TS_LANGUAGE = tree_sitter.Language(tsts.language_typescript())
TSX_LANGUAGE = tree_sitter.Language(tsts.language_tsx())

_PARSERS: dict[str, tree_sitter.Parser] = {
    "python": tree_sitter.Parser(PY_LANGUAGE),
    "javascript": tree_sitter.Parser(JS_LANGUAGE),
    "typescript": tree_sitter.Parser(TS_LANGUAGE),
    "tsx": tree_sitter.Parser(TSX_LANGUAGE),
}

EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", "vendor", ".mypy_cache", ".pytest_cache", ".next", "coverage",
}
LOCKFILES = {
    "poetry.lock", "Pipfile.lock", "uv.lock",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
}


def language_for(path: str) -> str | None:
    if path.endswith((".py", ".pyi")):
        return "python"
    if path.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "javascript"
    if path.endswith(".ts"):
        return "typescript"
    if path.endswith(".tsx"):
        return "tsx"
    return None


def is_excluded(path: str) -> bool:
    p = PurePosixPath(path)
    if p.name in LOCKFILES:
        return True
    if any(part in EXCLUDED_DIRS for part in p.parts[:-1]):
        return True
    if p.name.endswith(("_pb2.py", "_pb2_grpc.py", ".min.js", ".min.css", ".d.ts")):
        return True
    return False


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    if any(part in {"test", "tests", "__tests__"} for part in p.parts[:-1]):
        return True
    return (
        p.name.startswith("test_")
        or p.name.endswith((
            "_test.py", "tests.py",
            ".test.js", ".spec.js", ".test.jsx", ".spec.jsx",
            ".test.ts", ".spec.ts", ".test.tsx", ".spec.tsx",
        ))
    )


def _slice(src: bytes, node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _norm_hash(src: bytes, node) -> str:
    tokens: list[str] = []

    def collect(n):
        if n.type == "comment":
            return
        if not n.children:
            text = _slice(src, n).strip()
            if text:
                tokens.append(text)
            return
        for child in n.children:
            collect(child)

    collect(node)
    return hashlib.sha256("\x1f".join(tokens).encode("utf-8")).hexdigest()[:16]


def _extract_signature(src: bytes, def_node) -> str:
    body = def_node.child_by_field_name("body")
    end_byte = body.start_byte if body else def_node.end_byte
    header = src[def_node.start_byte:end_byte].decode("utf-8", "replace")
    return " ".join(header.split())


# ---------------------------------------------------------------------------
# Python AST extraction
# ---------------------------------------------------------------------------
def _py_arg_bounds(func_node, is_bound_method: bool) -> tuple[int, int | None]:
    params_node = func_node.child_by_field_name("parameters")
    if not params_node:
        return 0, 0
    min_args = 0
    max_args: int | None = 0
    unbounded = False
    for child in params_node.named_children:
        t = child.type
        if t in ("identifier", "typed_parameter"):
            min_args += 1
            if max_args is not None:
                max_args += 1
        elif t in ("default_parameter", "typed_default_parameter"):
            if max_args is not None:
                max_args += 1
        elif t in ("list_splat_pattern", "dictionary_splat_pattern"):
            unbounded = True
    if is_bound_method:
        min_args = max(0, min_args - 1)
        if max_args is not None:
            max_args = max(0, max_args - 1)
    return min_args, (None if unbounded else max_args)


def _py_count_call_args(call_node) -> int | None:
    args_node = call_node.child_by_field_name("arguments")
    if not args_node:
        return 0
    count = 0
    for child in args_node.named_children:
        if child.type in ("list_splat", "dictionary_splat"):
            return None
        count += 1
    return count


def _extract_python(path: str, src: bytes) -> dict:
    tree = _PARSERS["python"].parse(src)
    symbols: list[dict] = []
    refs: list[dict] = []

    def walk(node, scope_stack: list[tuple[str, str]]):
        if node.type == "decorated_definition":
            decorators = [_slice(src, c).strip() for c in node.children if c.type == "decorator"]
            inner = next(
                (c for c in node.children if c.type in ("function_definition", "class_definition")),
                None,
            )
            if inner is not None:
                _handle_def(inner, node, decorators, scope_stack)
                return

        if node.type in ("function_definition", "class_definition"):
            _handle_def(node, node, [], scope_stack)
            return

        if node.type == "call":
            func_node = node.child_by_field_name("function")
            if func_node is not None:
                full_call = _slice(src, func_node)
                qualifier = None
                if func_node.type == "attribute":
                    attr = func_node.child_by_field_name("attribute")
                    obj = func_node.child_by_field_name("object")
                    to_name = _slice(src, attr) if attr else full_call.rsplit(".", 1)[-1]
                    qualifier = _slice(src, obj) if obj else None
                else:
                    to_name = full_call

                caller_scope = scope_stack[-1][0] if scope_stack else "<module>"
                refs.append({
                    "path": path,
                    "from_symbol": f"{path}#{caller_scope}",
                    "to_name": to_name,
                    "called_name": full_call,
                    "qualifier": qualifier,
                    "line": node.start_point[0] + 1,
                    "arg_count": _py_count_call_args(node),
                })

        for child in node.children:
            walk(child, scope_stack)

    def _handle_def(def_node, outer_node, decorators: list[str], scope_stack: list[tuple[str, str]]):
        name_node = def_node.child_by_field_name("name")
        if not name_node:
            return
        short_name = _slice(src, name_node)
        parent_name, parent_kind = scope_stack[-1] if scope_stack else (None, None)
        qual_name = f"{parent_name}.{short_name}" if parent_name else short_name

        if def_node.type == "class_definition":
            kind = "class"
            min_args, max_args = 0, None
        else:
            kind = "method" if parent_kind == "class" else "function"
            is_static = any(d.startswith("@staticmethod") for d in decorators)
            min_args, max_args = _py_arg_bounds(def_node, is_bound_method=(kind == "method" and not is_static))

        symbols.append({
            "symbol_id": f"{path}#{qual_name}",
            "file": path,
            "name": qual_name,
            "short_name": short_name,
            "kind": kind,
            "start_line": outer_node.start_point[0] + 1,
            "end_line": outer_node.end_point[0] + 1,
            "signature": _extract_signature(src, def_node),
            "exported": not short_name.startswith("_"),
            "norm_hash": _norm_hash(src, outer_node),
            "min_args": min_args,
            "max_args": max_args,
        })

        body = def_node.child_by_field_name("body")
        if body is not None:
            next_stack = [*scope_stack, (qual_name, kind)]
            for child in body.children:
                walk(child, next_stack)

    walk(tree.root_node, [])
    return {"symbols": symbols, "refs": refs}


# ---------------------------------------------------------------------------
# JavaScript / TypeScript / TSX AST extraction
# ---------------------------------------------------------------------------
def _jsts_arg_bounds(func_node) -> tuple[int, int | None]:
    params_node = func_node.child_by_field_name("parameters")
    if not params_node:
        if func_node.child_by_field_name("parameter"):
            return 1, 1  # single-param arrow function: `x => x + 1`
        return 0, 0

    min_args = 0
    max_args: int | None = 0
    unbounded = False

    for child in params_node.named_children:
        t = child.type
        if t == "rest_pattern" or any(c.type == "rest_pattern" for c in child.children):
            unbounded = True
        elif t in ("optional_parameter", "assignment_pattern"):
            if max_args is not None:
                max_args += 1
        elif t == "required_parameter":
            has_default = any(c.type in ("assignment_pattern", "=") for c in child.children)
            if not has_default:
                min_args += 1
            if max_args is not None:
                max_args += 1
        elif t in ("identifier", "object_pattern", "array_pattern"):
            min_args += 1
            if max_args is not None:
                max_args += 1

    return min_args, (None if unbounded else max_args)


def _jsts_count_call_args(call_node) -> int | None:
    args_node = call_node.child_by_field_name("arguments")
    if not args_node:
        return 0
    count = 0
    for child in args_node.named_children:
        if child.type == "spread_element":
            return None
        count += 1
    return count


def _extract_jsts(path: str, src: bytes, lang: str) -> dict:
    tree = _PARSERS[lang].parse(src)
    symbols: list[dict] = []
    refs: list[dict] = []

    def walk(node, scope_stack: list[tuple[str, str]], is_exported: bool = False):
        if node.type == "export_statement":
            for child in node.children:
                if child.type in ("arrow_function", "function_expression", "function"):
                    _handle_named_def(child, node, True, scope_stack)
                else:
                    walk(child, scope_stack, is_exported=True)
            return

        if node.type in ("function_declaration", "generator_function_declaration", "class_declaration"):
            _handle_named_def(node, node, is_exported, scope_stack)
            return

        if node.type == "method_definition":
            _handle_method(node, scope_stack)
            return

        if node.type in ("lexical_declaration", "variable_declaration"):
            for decl in node.named_children:
                if decl.type != "variable_declarator":
                    continue
                name_node = decl.child_by_field_name("name")
                val_node = decl.child_by_field_name("value")
                if (
                    name_node
                    and name_node.type == "identifier"
                    and val_node
                    and val_node.type in ("arrow_function", "function_expression", "function")
                ):
                    _handle_var_func(name_node, val_node, node, is_exported, scope_stack)
                else:
                    walk(decl, scope_stack, False)
            return

        if node.type in ("call_expression", "new_expression"):
            callee = node.child_by_field_name("function") or node.child_by_field_name("constructor")
            if callee is not None:
                full_call = _slice(src, callee)
                qualifier = None
                if callee.type == "member_expression":
                    prop = callee.child_by_field_name("property")
                    obj = callee.child_by_field_name("object")
                    to_name = _slice(src, prop) if prop else full_call.rsplit(".", 1)[-1]
                    qualifier = _slice(src, obj) if obj else None
                else:
                    to_name = full_call

                if qualifier == "this":
                    qualifier = "self"  # normalize so IndexStore resolves same-class methods

                caller_scope = scope_stack[-1][0] if scope_stack else "<module>"
                refs.append({
                    "path": path,
                    "from_symbol": f"{path}#{caller_scope}",
                    "to_name": to_name,
                    "called_name": full_call,
                    "qualifier": qualifier,
                    "line": node.start_point[0] + 1,
                    "arg_count": _jsts_count_call_args(node),
                })

        for child in node.children:
            walk(child, scope_stack, False)

    def _handle_named_def(def_node, outer_node, exported: bool, scope_stack: list[tuple[str, str]]):
        name_node = def_node.child_by_field_name("name")
        short_name = _slice(src, name_node) if name_node else "default"
        parent_name, _ = scope_stack[-1] if scope_stack else (None, None)
        qual_name = f"{parent_name}.{short_name}" if parent_name else short_name

        if def_node.type == "class_declaration":
            kind = "class"
            min_args, max_args = 0, None
        else:
            kind = "function"
            min_args, max_args = _jsts_arg_bounds(def_node)

        symbols.append({
            "symbol_id": f"{path}#{qual_name}",
            "file": path,
            "name": qual_name,
            "short_name": short_name,
            "kind": kind,
            "start_line": outer_node.start_point[0] + 1,
            "end_line": outer_node.end_point[0] + 1,
            "signature": _extract_signature(src, def_node),
            "exported": exported,
            "norm_hash": _norm_hash(src, outer_node),
            "min_args": min_args,
            "max_args": max_args,
        })

        body = def_node.child_by_field_name("body")
        if body is not None:
            next_stack = [*scope_stack, (qual_name, kind)]
            for child in body.children:
                walk(child, next_stack, False)

    def _handle_method(method_node, scope_stack: list[tuple[str, str]]):
        name_node = method_node.child_by_field_name("name")
        if not name_node:
            return
        short_name = _slice(src, name_node)
        parent_name, _ = scope_stack[-1] if scope_stack else (None, None)
        qual_name = f"{parent_name}.{short_name}" if parent_name else short_name
        min_args, max_args = _jsts_arg_bounds(method_node)

        access_mod = next(
            (_slice(src, c) for c in method_node.children if c.type == "accessibility_modifier"),
            "public",
        )
        is_private = short_name.startswith("#") or access_mod in ("private", "protected")

        symbols.append({
            "symbol_id": f"{path}#{qual_name}",
            "file": path,
            "name": qual_name,
            "short_name": short_name,
            "kind": "method",
            "start_line": method_node.start_point[0] + 1,
            "end_line": method_node.end_point[0] + 1,
            "signature": _extract_signature(src, method_node),
            "exported": not is_private,
            "norm_hash": _norm_hash(src, method_node),
            "min_args": min_args,
            "max_args": max_args,
        })

        body = method_node.child_by_field_name("body")
        if body is not None:
            next_stack = [*scope_stack, (qual_name, "method")]
            for child in body.children:
                walk(child, next_stack, False)

    def _handle_var_func(name_node, func_node, outer_node, exported: bool, scope_stack: list[tuple[str, str]]):
        short_name = _slice(src, name_node)
        parent_name, _ = scope_stack[-1] if scope_stack else (None, None)
        qual_name = f"{parent_name}.{short_name}" if parent_name else short_name
        min_args, max_args = _jsts_arg_bounds(func_node)

        symbols.append({
            "symbol_id": f"{path}#{qual_name}",
            "file": path,
            "name": qual_name,
            "short_name": short_name,
            "kind": "function",
            "start_line": outer_node.start_point[0] + 1,
            "end_line": outer_node.end_point[0] + 1,
            "signature": _extract_signature(src, func_node),
            "exported": exported,
            "norm_hash": _norm_hash(src, outer_node),
            "min_args": min_args,
            "max_args": max_args,
        })

        body = func_node.child_by_field_name("body")
        if body is not None:
            next_stack = [*scope_stack, (qual_name, "function")]
            for child in body.children:
                walk(child, next_stack, False)

    walk(tree.root_node, [], False)
    return {"symbols": symbols, "refs": refs}


def extract(path: str, src: bytes | None) -> dict:
    if not src:
        return {"symbols": [], "refs": []}
    lang = language_for(path)
    if lang == "python":
        return _extract_python(path, src)
    if lang in ("javascript", "typescript", "tsx"):
        return _extract_jsts(path, src, lang)
    return {"symbols": [], "refs": []}
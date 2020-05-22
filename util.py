import functools
import json
import re
import subprocess
import time


DEBUG = False


def strip_debug_info(value):
    if type(value) == dict:
        return {k: v for k, v in value.items() if not k.startswith("_")}
    return value


def collect(collector=list):
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            generated_values = list(f(*args, **kwargs))

            if not DEBUG:
                generated_values = [
                    strip_debug_info(value) for value in generated_values
                ]

            if collector == list:
                return generated_values
            return collector(generated_values)

        return wrapper

    return decorator


def pp(obj):
    print(json.dumps(obj, indent=2))


def sh(command, disown=False, print_command=False):
    if disown:
        command = f"screen -d -m {command}"
    if print_command:
        print(command)

    return subprocess.run(command, shell=True, capture_output=True).stdout.decode(
        "utf-8"
    )


_NO_FALLBACK_VALUE = object()


def retry(
    predicate=None, error_message="Timed out.", fallback_value=_NO_FALLBACK_VALUE
):

    if predicate is None:
        predicate = lambda x: x is not None

    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            for i in range(1, 10):
                result = f(*args, **kwargs)
                if predicate(result):
                    return result
                time.sleep(i / 10)

            if fallback_value is not _NO_FALLBACK_VALUE:
                return fallback_value

            raise ValueError(error_message)

        return wrapper

    return decorator


def ps_tree():
    [_1, *rows, _2] = sh("ps ax -o pid -o ppid -o cmd").split("\n")

    nodes_by_pid = {}

    for row in rows:
        pid, parent_pid, command = re.search(
            r"\s*(\d+)\s*(\d+)\s*(.*)", row.strip()
        ).groups()

        nodes_by_pid[int(pid)] = {
            "pid": int(pid),
            "parent_pid": int(parent_pid),
            "command": command,
            "children": [],
        }

    for pid, node in nodes_by_pid.items():
        parent_node = nodes_by_pid.get(node["parent_pid"])
        if parent_node:
            parent_node["children"].append(node)

    forest = [
        node for node in nodes_by_pid.values() if node["parent_pid"] not in nodes_by_pid
    ]
    return forest


def eval_predicate(process, predicate):
    env = {
        "descendants": lambda: list(ps_tree_descendants(process)),
        "process": process,
    }
    return eval(predicate.strip(), env)


def ps_tree_query(tree, command=None, command_regex=None, predicate=None):
    for node in tree:
        if command is not None and node["command"] == command:
            yield node
        if command_regex is not None and re.search(command_regex, node["command"]):
            yield node
        if predicate is not None and eval_predicate(node, predicate):
            yield node

        yield from ps_tree_query(
            node["children"],
            command=command,
            command_regex=command_regex,
            predicate=predicate,
        )


def ps_tree_descendants(node):
    for child in node["children"]:
        yield child
        yield from ps_tree_descendants(child)

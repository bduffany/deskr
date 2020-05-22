import os
import re
import sys
import time
import yaml
from argparse import ArgumentParser
from util import collect, sh, ps_tree, ps_tree_query, ps_tree_descendants, pp, retry


@collect(list)
def get_connected_monitors():
    xrandr_output = sh("xrandr --query").split("\n")
    for line in xrandr_output:
        if " connected " not in line:
            continue
        monitor = {"_xrandr_raw": line}
        monitor["labels"] = ["primary" if " primary " in line else "secondary"]
        (width, height) = map(int, re.findall(r"\d+x\d+", line)[0].split("x"))
        (x, y) = map(int, re.findall(r"\+\d+", line))
        monitor.update({"width": width, "height": height, "x": x, "y": y})
        yield monitor


@collect(dict)
def get_monitors_by_label(monitors):
    for monitor in monitors:
        for label in monitor["labels"]:
            yield label, monitor


def to_pixels(value, relative_value):
    if type(value) == int:
        return value
    if type(value) != str:
        raise ValueError(f"Invalid pixel value specifier: {value}")
    if "%" in value:
        fraction = float(value[:-1]) / 100
        return int(relative_value * fraction)
    return int(value)


def get_absolute_location(window_spec, monitors_by_label):
    monitor = monitors_by_label[window_spec["monitor"]]

    position = window_spec["position"]
    top = to_pixels(position[0], monitor["height"])
    right = to_pixels(position[1], monitor["width"])
    bottom = to_pixels(position[2], monitor["height"])
    left = to_pixels(position[3], monitor["width"])

    return {
        "x": monitor["x"] + left,
        "y": monitor["y"] + top,
        "width": monitor["width"] - left - right,
        "height": monitor["height"] - top - bottom,
    }


def get_matching_window(window_spec):
    open_windows = None
    locator = window_spec["window_locator"]

    title_regex = locator.get("title_regex")
    if title_regex:
        open_windows = get_open_windows()
        for window in open_windows:
            if re.search(title_regex, window["title"]):
                return window

    pstree_query = locator.get("pstree")
    if pstree_query:
        try:
            window_process = next(ps_tree_query(ps_tree(), **pstree_query))
        except StopIteration:
            return None
        if not window_process:
            return None
        if not open_windows:
            open_windows = get_open_windows()
        for window in open_windows:
            if window["pid"] == window_process["pid"]:
                return window

    return None


def wait_for_window(window_spec):
    @retry(error_message=f"Timed out waiting for window to be spawned: {window_spec}")
    def get_window():
        return get_matching_window(window_spec)

    return get_window()


@retry(predicate=bool)
def poll(f):
    return f()


def check_deps(deps):
    polled = deps.get("poll", [])
    for dep in polled:
        poll(dep)
    once = deps.get("once", [])


def check_preconditions(preconditions):
    env = {
        "sh": sh,
        "poll": poll,
    }
    if not preconditions:
        return
    for precondition in preconditions:
        if not eval(precondition, env):
            raise (f"Precondition not met: {precondition}")


def execute_spec(window_spec, monitors_by_label):
    window = get_matching_window(window_spec)
    if not window:
        command_preconditions = window_spec.get("command_preconditions")
        check_preconditions(command_preconditions)
        sh(window_spec["command"], print_command=True, disown=True)
        window = wait_for_window(window_spec)

    rect = get_absolute_location(window_spec, monitors_by_label)
    wmctrl_location = f'0,{rect["x"]},{rect["y"]},{rect["width"]},{rect["height"]}'

    reposition_preconditions = window_spec.get("reposition_preconditions")
    check_preconditions(reposition_preconditions)

    sh(f'wmctrl -i -r {window["id"]} -e {wmctrl_location}', print_command=True)


@collect(dict)
def get_running_commands_by_pid():
    for line in sh("ps -aux").split("\n"):
        if "COMMAND" in line:
            command_index = line.index("COMMAND")
            continue
        if not line.strip():
            continue
        pid = int(re.search(r".*?\s+(\d+)", line).group(1))
        command = line[command_index:]
        yield pid, command


@collect(list)
def get_open_windows():
    # TODO: add -p flag to get PID and avoid xprop invocation.
    # Also use xwininfo -tree -root to get window title.
    lines = sh("wmctrl -l").split("\n")
    hostname = sh("hostname").strip()
    commands_by_pid = get_running_commands_by_pid()

    for line in lines:
        if not line.strip():
            continue
        title = re.sub(f".*? {hostname} ", "", line)
        window_id = re.findall(r"^0x[^\s]+", line)[0]
        xprop = sh(f"xprop -id {window_id}")
        pid = int(re.search(r"_NET_WM_PID\(CARDINAL\) = (\d+)", xprop).group(1))
        yield {
            "_wmctrl_raw": line,
            "_xprop_raw": xprop,
            "title": title,
            "id": window_id,
            "pid": pid,
            # pylint: disable=unsubscriptable-object
            "command": commands_by_pid[pid],
        }


def layout(config_path):
    monitors_by_label = get_monitors_by_label(get_connected_monitors())

    with open(config_path, "r") as config_file:
        config = yaml.load(config_file, Loader=yaml.FullLoader)

    for window_spec in config:
        execute_spec(window_spec, monitors_by_label)


def pstree():
    pp(ps_tree())


def test_locators(config_path):
    with open(config_path, "r") as config_file:
        config = yaml.load(config_file, Loader=yaml.FullLoader)

    for spec in config:
        tree = ps_tree()
        print("Testing locator:")
        pp(spec["window_locator"])
        try:
            result = next(ps_tree_query(tree, **spec["window_locator"]))
            if result["children"]:
                result["children"] = "[...]"
        except StopIteration:
            result = None
        pp(result)


def create_shortcut(config_path, icon_url=None):
    icon_url = (
        icon_url
        or "https://icons.iconarchive.com/icons/vexels/office/512/desktop-icon.png"
    )
    deskr_path = os.path.abspath(__file__)
    absolute_config_path = os.path.abspath(config_path)
    name = os.path.basename(config_path).replace(".deskr.yaml", "")
    desktop_folder = os.path.abspath(
        os.path.join(os.path.expanduser("~"), ".local/share/applications")
    )
    downloaded_icon_path = f"{desktop_folder}/{name}_icon.png"
    sh(f"wget -qO- {icon_url} > {downloaded_icon_path}")
    desktop_contents = f"""#!/usr/bin/env xdg-open
[Desktop Entry]
Version=1.0
Terminal=false
Type=Application
Name={name}
Exec=/usr/bin/env python3 {deskr_path} layout {absolute_config_path}
Icon={downloaded_icon_path}
"""
    desktop_path = os.path.join(desktop_folder, name + ".desktop")
    with open(desktop_path, "w") as desktop_file:
        print(f"Writing {desktop_path}")
        desktop_file.write(desktop_contents)
    sh(f"sudo chmod +x {desktop_path}", print_command=True)


if __name__ == "__main__":
    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="subparser")

    layout_parser = subparsers.add_parser("layout")
    layout_parser.add_argument(
        "config_path", type=str, help="Path to layout config (YAML)",
    )

    create_shortcut_parser = subparsers.add_parser("create-shortcut")
    create_shortcut_parser.add_argument(
        "config_path", type=str, help="Path to layout config (YAML)",
    )
    create_shortcut_parser.add_argument(
        "-i", "--icon-url", help="Url for the icon to use"
    )

    pstree_parser = subparsers.add_parser("pstree")

    test_locators_parser = subparsers.add_parser("test-locators")
    test_locators_parser.add_argument(
        "config_path", type=str, help="Path to layout config (YAML)",
    )

    kwargs = vars(parser.parse_args())
    globals()[kwargs.pop("subparser").replace("-", "_")](
        **{k.replace("-", "_"): v for k, v in kwargs.items()}
    )

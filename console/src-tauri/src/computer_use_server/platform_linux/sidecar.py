#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Linux platform process for the QwenPaw Computer Use helper.

The Rust helper keeps every shared rule (approval, freshness, input guard,
handoff, refresh) and forwards only the desktop calls here, one JSON line per
request on stdin and one per reply on stdout:

    {"id": 1, "op": "observe", "args": {...}}
    {"id": 1, "ok": true, "result": {...}}
    {"id": 1, "ok": false, "error": {"code": "...", "message": "..."}}

Accessibility comes from AT-SPI, windows from EWMH properties read with Xlib,
pixels from Gdk, and keyboard and pointer input from XTEST. Only X11 sessions are supported.
Runs on the system Python 3 with its GObject bindings.
"""

import base64
import ctypes
import ctypes.util
import json
import os
import sys
import time
import traceback
from collections import OrderedDict

import gi

gi.require_version("Atspi", "2.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Atspi, Gdk, GdkPixbuf, GLib  # noqa: E402

# Limits shared with the other platforms (see state.rs).
MAX_ELEMENTS = 300
MAX_DEPTH = 40
DOC_TEXT_MAX = 4000
SCREENSHOT_MAX_EDGE = 1600
JPEG_QUALITY = "80"
# Linux-specific bounds.
MAX_VISITED = 4000
MAX_CHILDREN = 1000
MAX_TRANSIENT = 3
MAX_VALUE_CHARS = 200
CACHED_OBSERVATIONS = 16

S = Atspi.StateType
WINDOW_TYPES = ("normal", "dialog", "utility")
TOP_LEVEL_ROLES = (
    "frame",
    "dialog",
    "file chooser",
    "alert",
    "color chooser",
    "font chooser",
    "window",
)
POPUP_ROLES = ("window", "menu", "popup menu")
# Roles whose listed actions make an unnamed node worth publishing, the way a
# UIA Invoke pattern does on Windows. Unnamed table cells and panels expose
# generic actions too, and would otherwise flood the element list.
INTERACTIVE_ROLES = {
    "push button",
    "toggle button",
    "check box",
    "radio button",
    "menu item",
    "check menu item",
    "radio menu item",
    "menu",
    "combo box",
    "text",
    "entry",
    "password text",
    "spin button",
    "slider",
    "link",
    "page tab",
    "list item",
    "tree item",
    "toggle",
    "split button",
}
# The activation verbs ``invoke`` may perform, in order of preference. Each
# toolkit names its AT-SPI actions freely (GTK also exposes keybinding signals
# such as "show_location"), so only the primary action from this list is
# published and invoked: the counterpart of the single Invoke action listed on
# Windows and the primary AX action used on macOS.
PRIMARY_ACTIONS = (
    "click",
    "press",
    "activate",
    "toggle",
    "jump",
    "open",
    "check",
    "uncheck",
)

KEY_NAMES = {
    "CTRL": "Control_L",
    "CONTROL": "Control_L",
    "ALT": "Alt_L",
    "SHIFT": "Shift_L",
    "WIN": "Super_L",
    "SUPER": "Super_L",
    "META": "Super_L",
    "LWIN": "Super_L",
    "RWIN": "Super_R",
    "ENTER": "Return",
    "RETURN": "Return",
    "TAB": "Tab",
    "ESC": "Escape",
    "ESCAPE": "Escape",
    "SPACE": "space",
    "BACKSPACE": "BackSpace",
    "DELETE": "Delete",
    "DEL": "Delete",
    "INSERT": "Insert",
    "INS": "Insert",
    "UP": "Up",
    "DOWN": "Down",
    "LEFT": "Left",
    "RIGHT": "Right",
    "HOME": "Home",
    "END": "End",
    "PAGEUP": "Prior",
    "PGUP": "Prior",
    "PAGEDOWN": "Next",
    "PGDN": "Next",
    "CAPSLOCK": "Caps_Lock",
    "NUMLOCK": "Num_Lock",
    "SCROLLLOCK": "Scroll_Lock",
    "PRINTSCREEN": "Print",
    "PRTSC": "Print",
    "PAUSE": "Pause",
    "BREAK": "Pause",
    "APPS": "Menu",
    "MENU": "Menu",
    "CONTEXTMENU": "Menu",
    "MULTIPLY": "KP_Multiply",
    "ADD": "KP_Add",
    "SUBTRACT": "KP_Subtract",
    "DECIMAL": "KP_Decimal",
    "DIVIDE": "KP_Divide",
}


class Failure(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


STALE = "Observation is stale or already consumed; observe the window again and use its new observation_id."


# ---------------------------------------------------------------- X11 ------


class XScreenSaverInfo(ctypes.Structure):
    _fields_ = [
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("til_or_since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong),
        ("eventMask", ctypes.c_ulong),
    ]


class XClientMessageData(ctypes.Union):
    _fields_ = [
        ("b", ctypes.c_char * 20),
        ("s", ctypes.c_short * 10),
        ("l", ctypes.c_long * 5),
    ]


class XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", XClientMessageData),
    ]


class XEvent(ctypes.Union):
    _fields_ = [("xclient", XClientMessageEvent), ("pad", ctypes.c_long * 24)]


X_ERROR_HANDLER = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_void_p,
)
# A window can vanish between listing and reading it. The default Xlib error
# handler would exit the process on that BadWindow, so errors are ignored and
# the failed call simply returns nothing.
_ignore_x_errors = X_ERROR_HANDLER(lambda display, event: 0)
CLIENT_MESSAGE = 33
SUBSTRUCTURE_MASK = (1 << 19) | (1 << 20)


class X11(object):
    """Window properties, XTEST input and idle time over a private Xlib connection.

    Every query goes to the X server: a window id can be reused by a new
    window, so nothing about a window is cached between requests.
    """

    def __init__(self):
        self.xlib = ctypes.CDLL(
            ctypes.util.find_library("X11") or "libX11.so.6",
        )
        self.xtst = ctypes.CDLL(
            ctypes.util.find_library("Xtst") or "libXtst.so.6",
        )
        try:
            self.xss = ctypes.CDLL(
                ctypes.util.find_library("Xss") or "libXss.so.1",
            )
        except OSError:
            self.xss = None
        x, t = self.xlib, self.xtst
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x.XStringToKeysym.restype = ctypes.c_ulong
        x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XKeysymToKeycode.restype = ctypes.c_ubyte
        x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        t.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        t.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        t.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        x.XInternAtom.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        x.XInternAtom.restype = ctypes.c_ulong
        x.XGetWindowProperty.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_long,
            ctypes.c_long,
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        x.XGetWindowProperty.restype = ctypes.c_int
        x.XFree.argtypes = [ctypes.c_void_p]
        x.XGetGeometry.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x.XGetGeometry.restype = ctypes.c_int
        x.XTranslateCoordinates.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
        ]
        x.XTranslateCoordinates.restype = ctypes.c_int
        x.XSendEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_long,
            ctypes.POINTER(XEvent),
        ]
        x.XSetErrorHandler.argtypes = [X_ERROR_HANDLER]
        x.XSetErrorHandler.restype = ctypes.c_void_p
        x.XSetErrorHandler(_ignore_x_errors)
        self.display = x.XOpenDisplay(None)
        if not self.display:
            raise Failure(
                "accessibility_permission_required",
                "Cannot open the X display.",
            )
        self.root = x.XDefaultRootWindow(self.display)
        self.atoms = {}
        if self.xss is not None:
            self.xss.XScreenSaverQueryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(XScreenSaverInfo),
            ]
            self.xss.XScreenSaverQueryInfo.restype = ctypes.c_int

    def atom(self, name):
        if name not in self.atoms:
            self.atoms[name] = self.xlib.XInternAtom(
                self.display,
                name.encode("ascii"),
                0,
            )
        return self.atoms[name]

    def prop(self, window, name):
        """Return a property as a list of numbers (format 16/32) or bytes."""
        kind, fmt, count, after = (
            ctypes.c_ulong(),
            ctypes.c_int(),
            ctypes.c_ulong(),
            ctypes.c_ulong(),
        )
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.xlib.XGetWindowProperty(
            self.display,
            int(window),
            self.atom(name),
            0,
            4096,
            0,
            0,
            ctypes.byref(kind),
            ctypes.byref(fmt),
            ctypes.byref(count),
            ctypes.byref(after),
            ctypes.byref(data),
        )
        if status != 0 or not data:
            return None
        try:
            if fmt.value == 32:
                return list(
                    ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[
                        : count.value
                    ],
                )
            if fmt.value == 16:
                return list(
                    ctypes.cast(data, ctypes.POINTER(ctypes.c_ushort))[
                        : count.value
                    ],
                )
            return ctypes.string_at(data, count.value)
        finally:
            self.xlib.XFree(data)

    def text(self, window):
        value = self.prop(window, "_NET_WM_NAME")
        if isinstance(value, bytes) and value:
            return value.decode("utf-8", "replace")
        value = self.prop(window, "WM_NAME")
        return value.decode("latin-1") if isinstance(value, bytes) else ""

    def client_geometry(self, window):
        root, gx, gy = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_int()
        width, height, border, depth = (
            ctypes.c_uint(),
            ctypes.c_uint(),
            ctypes.c_uint(),
            ctypes.c_uint(),
        )
        if not self.xlib.XGetGeometry(
            self.display,
            int(window),
            ctypes.byref(root),
            ctypes.byref(gx),
            ctypes.byref(gy),
            ctypes.byref(width),
            ctypes.byref(height),
            ctypes.byref(border),
            ctypes.byref(depth),
        ):
            return None
        x, y, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
        if not self.xlib.XTranslateCoordinates(
            self.display,
            int(window),
            self.root,
            0,
            0,
            ctypes.byref(x),
            ctypes.byref(y),
            ctypes.byref(child),
        ):
            return None
        return [x.value, y.value, int(width.value), int(height.value)]

    def send_client_message(self, window, name, values):
        event = XEvent()
        event.xclient.type = CLIENT_MESSAGE
        event.xclient.send_event = 1
        event.xclient.display = self.display
        event.xclient.window = int(window)
        event.xclient.message_type = self.atom(name)
        event.xclient.format = 32
        for index, value in enumerate(values[:5]):
            event.xclient.data.l[index] = value
        self.xlib.XSendEvent(
            self.display,
            self.root,
            0,
            SUBSTRUCTURE_MASK,
            ctypes.byref(event),
        )
        self.sync()

    def keycode(self, keysym_name):
        keysym = self.xlib.XStringToKeysym(keysym_name.encode("ascii"))
        if not keysym:
            return 0
        return self.xlib.XKeysymToKeycode(self.display, keysym)

    def key(self, keycode, press):
        self.xtst.XTestFakeKeyEvent(
            self.display,
            keycode,
            1 if press else 0,
            0,
        )

    def move(self, x, y):
        self.xtst.XTestFakeMotionEvent(self.display, -1, int(x), int(y), 0)

    def button(self, number, press):
        self.xtst.XTestFakeButtonEvent(
            self.display,
            number,
            1 if press else 0,
            0,
        )

    def sync(self):
        self.xlib.XSync(self.display, 0)

    def idle_ms(self):
        if self.xss is None:
            return None
        info = XScreenSaverInfo()
        root = self.xlib.XDefaultRootWindow(self.display)
        if not self.xss.XScreenSaverQueryInfo(
            self.display,
            root,
            ctypes.byref(info),
        ):
            return None
        return int(info.idle)


X = None
OBSERVATIONS = OrderedDict()


def pump():
    context = GLib.MainContext.default()
    for _ in range(500):
        if not context.pending():
            break
        context.iteration(False)


# ------------------------------------------------------------- windows -----


class Win(object):
    """A managed top-level window, read from the X server when created."""

    def __init__(self, xid):
        self.xid = int(xid)
        pid = X.prop(self.xid, "_NET_WM_PID")
        self.pid = int(pid[0]) if isinstance(pid, list) and pid else 0
        self.title = X.text(self.xid)
        wm_class = X.prop(self.xid, "WM_CLASS")
        parts = wm_class.split(b"\0") if isinstance(wm_class, bytes) else []
        self.wm_class = (
            parts[1].decode("utf-8", "replace") if len(parts) > 1 else ""
        )
        transient = X.prop(self.xid, "WM_TRANSIENT_FOR")
        self.transient_for = (
            int(transient[0])
            if isinstance(transient, list) and transient
            else 0
        )
        types = X.prop(self.xid, "_NET_WM_WINDOW_TYPE")
        self.kind = self._kind(types if isinstance(types, list) else [])
        state = X.prop(self.xid, "_NET_WM_STATE")
        self.hidden = (
            isinstance(state, list) and X.atom("_NET_WM_STATE_HIDDEN") in state
        )

    def _kind(self, types):
        for name in (
            "NORMAL",
            "DIALOG",
            "UTILITY",
            "DOCK",
            "DESKTOP",
            "MENU",
            "TOOLBAR",
            "SPLASH",
            "POPUP_MENU",
            "DROPDOWN_MENU",
            "TOOLTIP",
            "NOTIFICATION",
        ):
            if X.atom("_NET_WM_WINDOW_TYPE_" + name) in types:
                return name.lower()
        return "dialog" if self.transient_for else "normal"

    def get_xid(self):
        return self.xid

    def get_pid(self):
        return self.pid

    def get_name(self):
        return self.title

    def has_name(self):
        return bool(self.title)

    def get_client_window_geometry(self):
        return X.client_geometry(self.xid) or [0, 0, 0, 0]


def client_list():
    value = X.prop(X.root, "_NET_CLIENT_LIST")
    return [int(xid) for xid in value] if isinstance(value, list) else []


def window_record(window):
    return {
        "xid": window.get_xid(),
        "pid": window.get_pid(),
        "title": window.get_name(),
        "wm_class": window.wm_class,
    }


def managed_windows():
    windows = [Win(xid) for xid in client_list()]
    return [w for w in windows if w.kind in WINDOW_TYPES]


def find_window(xid):
    if int(xid) in client_list():
        window = Win(xid)
        if window.kind in WINDOW_TYPES:
            return window
    raise Failure("window_not_found", "Target window was not found.")


def geometry(window):
    """Frame geometry: the client area plus the window manager's decorations."""
    client = X.client_geometry(window.get_xid())
    if client is None:
        raise Failure("window_not_found", "Target window no longer exists.")
    extents = X.prop(window.get_xid(), "_NET_FRAME_EXTENTS")
    left, right, top, bottom = (
        (list(extents) + [0, 0, 0, 0])[:4]
        if isinstance(extents, list)
        else (0, 0, 0, 0)
    )
    return [
        client[0] - left,
        client[1] - top,
        client[2] + left + right,
        client[3] + top + bottom,
    ]


def active_xid():
    value = X.prop(X.root, "_NET_ACTIVE_WINDOW")
    return int(value[0]) if isinstance(value, list) and value else 0


def is_active(window):
    active = active_xid()
    if not active:
        return False
    if active == window.get_xid():
        return True
    # A modal dialog the target owns counts as the target, as on Windows.
    transient = X.prop(active, "WM_TRANSIENT_FOR")
    return (
        isinstance(transient, list)
        and bool(transient)
        and int(transient[0]) == window.get_xid()
    )


def activate(window):
    if is_active(window):
        return
    # Source 2 marks a pager-style request, which window managers honour
    # without focus-stealing prevention; activation also restores a
    # minimized window.
    X.send_client_message(
        window.get_xid(),
        "_NET_ACTIVE_WINDOW",
        [2, 0, 0, 0, 0],
    )
    for _ in range(40):
        time.sleep(0.025)
        if is_active(window):
            return
    raise Failure("focus_failed", "Could not activate the target window.")


def op_windows(args):
    return [window_record(w) for w in managed_windows()]


def op_active(args):
    active = active_xid()
    if not active or active not in client_list():
        return None
    window = Win(active)
    if window.kind not in WINDOW_TYPES:
        return None
    return window_record(window)


def op_window(args):
    return window_record(find_window(args["xid"]))


def op_close(args):
    window = find_window(args["xid"])
    xid = window.get_xid()
    X.send_client_message(xid, "_NET_CLOSE_WINDOW", [0, 2, 0, 0, 0])
    for _ in range(40):
        time.sleep(0.05)
        if xid not in client_list():
            return {"closed": True}
    return {"closed": False}


# -------------------------------------------------------- accessibility ----


def fresh(node):
    """Drop libatspi's cached values for a node and its descendants.

    Applications only send change events to registered listeners, so a cached
    state or name would go stale between requests. Clearing is enough; turning
    the application's cache off instead (set_cache_mask) makes the next child
    query hang in at-spi2-core 2.40.
    """
    try:
        node.clear_cache()
    except Exception:
        pass
    return node


def extents(node):
    try:
        rect = node.get_extents(Atspi.CoordType.SCREEN)
        return [int(rect.x), int(rect.y), int(rect.width), int(rect.height)]
    except Exception:
        return None


def find_app(pid):
    desktop = Atspi.get_desktop(0)
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is None:
            continue
        try:
            if app.get_process_id() == pid:
                return fresh(app)
        except Exception:
            continue
    return None


def top_levels(app):
    nodes = []
    try:
        count = app.get_child_count()
    except Exception:
        return nodes
    for index in range(count):
        node = app.get_child_at_index(index)
        if node is None:
            continue
        try:
            role = node.get_role_name()
            states = node.get_state_set()
        except Exception:
            continue
        nodes.append((node, role, states))
    return nodes


def distance(a, b):
    if a is None or b is None:
        return 10**9
    return sum(abs(x - y) for x, y in zip(a, b))


def find_frame(window):
    """The AT-SPI top level for a managed window, by owner, title and place."""
    app = find_app(window.get_pid())
    if app is None:
        return None, None, []
    title = window.get_name() if window.has_name() else ""
    client = list(window.get_client_window_geometry())
    best, best_score = None, None
    tops = top_levels(app)
    for node, role, states in tops:
        if role not in TOP_LEVEL_ROLES:
            continue
        try:
            name = node.get_name() or ""
        except Exception:
            name = ""
        if role == "window" and name != title:
            continue
        score = (
            0 if name == title else 1,
            0 if states.contains(S.SHOWING) else 1,
            distance(extents(node), client),
        )
        if best_score is None or score < best_score:
            best, best_score = node, score
    popups = []
    for node, role, states in tops:
        if (
            node is best
            or role not in POPUP_ROLES
            or not states.contains(S.SHOWING)
        ):
            continue
        try:
            if role == "window" and (node.get_name() or ""):
                continue
        except Exception:
            continue
        box = extents(node)
        if box and box[2] > 0 and box[3] > 0:
            popups.append((node, box))
        if len(popups) >= MAX_TRANSIENT:
            break
    return app, best, popups


def action_names(node):
    try:
        action = node.get_action_iface()
        if action is None:
            return []
        return [
            action.get_action_name(i) for i in range(action.get_n_actions())
        ]
    except Exception:
        return []


def primary_action(names):
    """Return ``(index, name)`` of the action ``invoke`` performs, or None."""
    lowered = [name.lower() for name in names]
    for wanted in PRIMARY_ACTIONS:
        if wanted in lowered:
            index = lowered.index(wanted)
            return index, names[index]
    return None


def has_editable_text(node):
    try:
        return node.get_editable_text_iface() is not None
    except Exception:
        return False


def value_iface(node):
    try:
        return node.get_value_iface()
    except Exception:
        return None


def text_of(node, limit=None):
    try:
        text = node.get_text_iface()
        if text is None:
            return None
        count = text.get_character_count()
        end = count if limit is None else min(count, limit)
        return text.get_text(0, end)
    except Exception:
        return None


def truncate_document_text(text):
    if len(text) <= DOC_TEXT_MAX:
        return text
    return text[:DOC_TEXT_MAX] + "… (truncated)"


def element_value(node, role, states):
    if role == "password text":
        return None
    if role in ("text", "entry") and states.contains(S.SINGLE_LINE):
        value = text_of(node, MAX_VALUE_CHARS)
        return value if value else None
    if role in ("spin button", "slider"):
        iface = value_iface(node)
        if iface is not None:
            try:
                number = iface.get_current_value()
                return (
                    ("%d" % number)
                    if float(number).is_integer()
                    else ("%g" % number)
                )
            except Exception:
                return None
    return None


class Collector(object):
    def __init__(self):
        self.records = []
        self.nodes = []
        self.visited = 0
        self.focused = None

    def publish(self, node, depth, role, states):
        try:
            name = node.get_name() or ""
        except Exception:
            name = ""
        identifier = ""
        getter = getattr(node, "get_accessible_id", None)
        if getter is not None:
            try:
                identifier = getter() or ""
            except Exception:
                identifier = ""
        focused = states.contains(S.FOCUSED)
        primary = primary_action(action_names(node))
        actions = [primary[1]] if primary else []
        settable = (
            (states.contains(S.EDITABLE) and has_editable_text(node))
            or (
                role in ("spin button", "slider")
                and value_iface(node) is not None
            )
        ) and not states.contains(S.READ_ONLY)
        if not (
            name
            or identifier
            or focused
            or settable
            or (actions and role in INTERACTIVE_ROLES)
        ):
            return
        if len(self.records) >= MAX_ELEMENTS:
            if not focused:
                return
            self.records.pop()
            self.nodes.pop()
        element_id = "atspi-%d" % len(self.records)
        box = extents(node) or [0, 0, 0, 0]
        record = {
            "id": element_id,
            "name": name,
            "role": role,
            "depth": depth,
            "enabled": states.contains(S.ENABLED)
            or states.contains(S.SENSITIVE),
            "offscreen": not states.contains(S.SHOWING),
            "selected": states.contains(S.SELECTED),
            "settable": bool(settable),
            "focused": focused,
            "actions": actions,
            "bounds": [box[0], box[1], box[0] + box[2], box[1] + box[3]],
        }
        if identifier:
            record["identifier"] = identifier
        value = element_value(node, role, states)
        if value is not None:
            record["value"] = value
        self.records.append(record)
        self.nodes.append(node)
        if focused:
            self.focused = (record, node)

    def visit(self, node, depth):
        if self.visited >= MAX_VISITED or depth > MAX_DEPTH:
            return
        self.visited += 1
        try:
            role = node.get_role_name()
            states = node.get_state_set()
        except Exception:
            return
        if depth > 0 and not states.contains(S.SHOWING):
            return
        self.publish(node, depth, role, states)
        if depth == MAX_DEPTH:
            return
        try:
            count = min(node.get_child_count(), MAX_CHILDREN)
        except Exception:
            return
        for index in range(count):
            if len(self.records) >= MAX_ELEMENTS and self.focused is not None:
                return
            try:
                child = node.get_child_at_index(index)
            except Exception:
                continue
            if child is not None:
                self.visit(child, depth + 1)


def collect(frame, popups):
    collector = Collector()
    if frame is None:
        return {
            "available": False,
            "reason": "The window exposes no accessibility tree.",
            "elements": [],
        }, []
    collector.visit(fresh(frame), 0)
    for node, _box in popups:
        collector.visit(fresh(node), 0)
    accessibility = {"available": True}
    if collector.focused is not None:
        record, node = collector.focused
        accessibility["focused_element"] = '%s %s "%s"' % (
            record["id"],
            record["role"],
            record["name"],
        )
        text = text_of(node, DOC_TEXT_MAX + 1)
        if text:
            accessibility["document_text"] = truncate_document_text(text)
    accessibility["elements"] = collector.records
    return accessibility, collector.nodes


# ------------------------------------------------------------- capture -----


def bounded(width, height):
    longest = max(width, height)
    if longest <= SCREENSHOT_MAX_EDGE or longest == 0:
        return width, height
    scale = float(SCREENSHOT_MAX_EDGE) / longest
    return max(1, int(round(width * scale))), max(
        1,
        int(round(height * scale)),
    )


def capture(box):
    root = Gdk.get_default_root_window()
    x, y, width, height = box
    left, top = max(0, x), max(0, y)
    right = min(root.get_width(), x + width)
    bottom = min(root.get_height(), y + height)
    if right <= left or bottom <= top:
        raise Failure("capture_failed", "The window is outside the screen.")
    box = [left, top, right - left, bottom - top]
    pixbuf = Gdk.pixbuf_get_from_window(root, box[0], box[1], box[2], box[3])
    if pixbuf is None:
        raise Failure("capture_failed", "The screen could not be read.")
    width, height = bounded(box[2], box[3])
    if (width, height) != (box[2], box[3]):
        pixbuf = pixbuf.scale_simple(
            width,
            height,
            GdkPixbuf.InterpType.BILINEAR,
        )
    ok, data = pixbuf.save_to_bufferv("jpeg", ["quality"], [JPEG_QUALITY])
    if not ok:
        raise Failure("capture_failed", "The screenshot could not be encoded.")
    return {
        "bounds": box,
        "width": width,
        "height": height,
        "data": base64.b64encode(data).decode("ascii"),
    }


def op_observe(args):
    window = find_window(args["xid"])
    want_screenshot = bool(args.get("screenshot"))
    if want_screenshot:
        # The root window holds only what is on top, so raise the target first.
        activate(window)
    _app, frame, popups = find_frame(window)
    accessibility, nodes = collect(frame, popups)
    key = args.get("key") or ""
    if key:
        OBSERVATIONS[key] = {
            "xid": window.get_xid(),
            "pid": window.get_pid(),
            "nodes": nodes,
        }
        while len(OBSERVATIONS) > CACHED_OBSERVATIONS:
            OBSERVATIONS.popitem(last=False)
    result = {
        "bounds": geometry(window),
        "accessibility": accessibility,
        "screenshots": [],
        "related_surface_count": len(popups),
    }
    if want_screenshot:
        try:
            main = capture(geometry(window))
            main.update(
                {"kind": "main", "z_index": 0, "xid": window.get_xid()},
            )
            result["screenshots"].append(main)
            for index, (_node, box) in enumerate(popups):
                shot = capture(box)
                shot.update(
                    {
                        "kind": "transient",
                        "z_index": index + 1,
                        "xid": window.get_xid(),
                    },
                )
                result["screenshots"].append(shot)
        except Failure as failure:
            result["visual_failure"] = failure.message
    return result


def op_tree(args):
    window = find_window(args["xid"])
    _app, frame, popups = find_frame(window)
    accessibility, _nodes = collect(frame, popups)
    return {"accessibility": accessibility}


# -------------------------------------------------------------- actions ----


def observed_node(args):
    entry = OBSERVATIONS.get(args.get("key") or "")
    if entry is None:
        raise Failure("stale_observation", STALE)
    index = args.get("element")
    if not isinstance(index, int) or index < 0 or index >= len(entry["nodes"]):
        raise Failure(
            "element_not_found",
            "Element is not available in this observation.",
        )
    node = fresh(entry["nodes"][index])
    try:
        states = node.get_state_set()
    except Exception:
        raise Failure(
            "stale_observation",
            "The element is no longer part of the observed window; observe it again.",
        )
    if states.contains(S.DEFUNCT):
        raise Failure(
            "stale_observation",
            "The element is no longer part of the observed window; observe it again.",
        )
    if not (states.contains(S.ENABLED) or states.contains(S.SENSITIVE)):
        raise Failure("element_unavailable", "Element is no longer enabled.")
    return node, states


def ensure_geometry(window, expected):
    if expected and geometry(window) != list(expected):
        raise Failure(
            "stale_observation",
            "Window geometry changed; observe it again.",
        )


def inside(point, box):
    x, y = point
    return box[0] <= x < box[0] + box[2] and box[1] <= y < box[1] + box[3]


def element_point(args, window):
    node, states = observed_node(args)
    if not states.contains(S.SHOWING):
        raise Failure(
            "element_unavailable",
            "Element is offscreen; scroll it into view before acting on it.",
        )
    box = extents(node)
    if not box or box[2] <= 0 or box[3] <= 0:
        raise Failure(
            "element_unavailable",
            "The element has no clickable area.",
        )
    point = (box[0] + box[2] // 2, box[1] + box[3] // 2)
    surfaces = [geometry(window)]
    if not inside(point, surfaces[0]):
        # Menus and drop-downs may extend past the window they belong to.
        _app, _frame, popups = find_frame(window)
        surfaces.extend(popup_box for _node, popup_box in popups)
    if not any(inside(point, surface) for surface in surfaces):
        raise Failure(
            "target_not_at_point",
            "The observed surface is no longer at this point.",
        )
    return point


def surface_point(args, window):
    surface = args.get("surface") or {}
    bounds = surface.get("bounds")
    if bounds and list(bounds) == list(args.get("window_bounds") or []):
        ensure_geometry(window, bounds)
    return (int(args["x"]), int(args["y"]))


def target_window(args):
    window = find_window(args["window"])
    ensure_geometry(window, args.get("window_bounds"))
    activate(window)
    ensure_geometry(window, args.get("window_bounds"))
    return window


BUTTONS = {"left": 1, "middle": 2, "right": 3}


def op_click(args):
    window = target_window(args)
    point = (
        element_point(args, window)
        if "element" in args
        else surface_point(args, window)
    )
    number = BUTTONS[args.get("button") or "left"]
    X.move(point[0], point[1])
    X.sync()
    time.sleep(0.03)
    for index in range(int(args.get("count") or 1)):
        if index:
            time.sleep(0.04)
        X.button(number, True)
        X.button(number, False)
    X.sync()
    return {"applied": True}


def op_scroll(args):
    window = target_window(args)
    point = surface_point(args, window)
    delta = int(args.get("delta_y") or 0)
    X.move(point[0], point[1])
    notches = max(1, int(round(abs(delta) / 120.0))) if delta else 0
    number = 4 if delta > 0 else 5
    for _ in range(notches):
        X.button(number, True)
        X.button(number, False)
    X.sync()
    return {"applied": True}


def op_drag(args):
    window = target_window(args)
    if "source" in args:
        start = element_point(args["source"], window)
        end = element_point(args["target"], window)
    else:
        start = surface_point(
            dict(args["start"], window_bounds=args.get("window_bounds")),
            window,
        )
        end = surface_point(
            dict(args["end"], window_bounds=args.get("window_bounds")),
            window,
        )
    X.move(start[0], start[1])
    X.button(1, True)
    X.sync()
    time.sleep(0.08)
    for step in range(1, 13):
        X.move(
            start[0] + (end[0] - start[0]) * step // 12,
            start[1] + (end[1] - start[1]) * step // 12,
        )
        X.sync()
        time.sleep(0.016)
    time.sleep(0.08)
    X.button(1, False)
    X.sync()
    return {"applied": True}


def parse_keys(value):
    names = [part.strip() for part in value.split("+") if part.strip()]
    if not names or len(names) > 4:
        raise Failure(
            "invalid_request",
            "key must contain one to four key names.",
        )
    codes = []
    for raw in names:
        name = raw.upper()
        if name in KEY_NAMES:
            keysym = KEY_NAMES[name]
        elif (
            name.startswith("F")
            and name[1:].isdigit()
            and 1 <= int(name[1:]) <= 24
        ):
            keysym = name
        elif (
            len(name) == 7 and name.startswith("NUMPAD") and name[6].isdigit()
        ):
            keysym = "KP_" + name[6]
        elif len(name) == 1 and name.isalnum() and ord(name) < 128:
            keysym = name.lower()
        else:
            raise Failure("invalid_request", "Unsupported key: %s." % raw)
        code = X.keycode(keysym)
        if not code:
            raise Failure(
                "invalid_request",
                "Key %s is not on the current keyboard layout." % raw,
            )
        codes.append(code)
    return codes


def send_keys(codes):
    for code in codes:
        X.key(code, True)
    for code in reversed(codes):
        X.key(code, False)
    X.sync()


def send_text(text):
    """Type text; line breaks and tabs are sent as their keys."""
    segment = []

    def flush():
        if segment:
            if not Atspi.generate_keyboard_event(
                0,
                "".join(segment),
                Atspi.KeySynthType.STRING,
            ):
                raise Failure("input_failed", "The text could not be typed.")
            del segment[:]

    for char in text:
        if char in "\n\t":
            flush()
            send_keys([X.keycode("Return" if char == "\n" else "Tab")])
        elif char != "\r":
            segment.append(char)
    flush()
    X.sync()


def focused_editable(args, window):
    entry = OBSERVATIONS.get(args.get("key") or "")
    if entry is not None:
        for node in entry["nodes"]:
            try:
                states = fresh(node).get_state_set()
            except Exception:
                continue
            if states.contains(S.FOCUSED):
                if not (
                    states.contains(S.EDITABLE) or has_editable_text(node)
                ):
                    raise Failure(
                        "focus_not_editable",
                        "The focused element does not expose text editing capabilities.",
                    )
                return node
    _app, frame, _popups = find_frame(window)
    if frame is not None:
        probe = Collector()
        probe.visit(fresh(frame), 0)
        if probe.focused is not None:
            raise Failure(
                "stale_observation",
                "Keyboard focus changed after the window was observed; observe it again before typing.",
            )
    raise Failure(
        "focus_not_editable",
        "The observed window has no focused text input.",
    )


def op_type(args):
    window = target_window(args)
    node = focused_editable(args, window)
    before = text_of(node)
    text = args["text"]
    send_text(text)
    probe = text.replace("\r", "").split("\n")[-1] or text.strip()
    observed = False
    for _ in range(4):
        after = text_of(node)
        observed = (
            after is not None
            and after != before
            and (not probe or probe in after)
        )
        if observed:
            break
        time.sleep(0.025)
    return {
        "applied": True,
        "effect": "observed" if observed else "unverified",
        "text_length": len(text),
    }


def op_key(args):
    codes = parse_keys(args["keys"])
    target_window(args)
    send_keys(codes)
    return {"applied": True, "key": args["keys"]}


def op_sequence(args):
    steps = args.get("steps") or []
    parsed = []
    for step in steps:
        if step.get("action") == "press_key":
            parsed.append(("key", parse_keys(step.get("key") or "")))
        else:
            parsed.append(("type", step.get("text") or ""))
    target_window(args)
    for index, (kind, value) in enumerate(parsed):
        try:
            if kind == "key":
                send_keys(value)
            else:
                send_text(value)
        except Failure as failure:
            return {
                "completed_steps": index,
                "error": {
                    "code": "input_failed",
                    "message": failure.message,
                    "step_index": index,
                    "outcome": "unknown",
                },
            }
    return {"applied": True, "completed_steps": len(parsed)}


def op_invoke(args):
    node, _states = observed_node(args)
    try:
        action = node.get_action_iface()
    except Exception:
        action = None
    primary = primary_action(action_names(node))
    if action is None or primary is None:
        raise Failure(
            "unsupported_operation",
            "The element does not expose a supported primary action.",
        )
    try:
        done = action.do_action(primary[0])
    except Exception as error:
        raise Failure("action_failed", "AT-SPI invoke failed: %s" % error)
    if not done:
        raise Failure("action_failed", "The application refused the action.")
    return {"applied": True}


def op_set_value(args):
    node, states = observed_node(args)
    value = args["value"]
    if states.contains(S.EDITABLE) and has_editable_text(node):
        try:
            node.get_editable_text_iface().set_text_contents(value)
        except Exception as error:
            raise Failure(
                "action_failed",
                "AT-SPI value update failed: %s" % error,
            )
        actual = text_of(fresh(node))
        if actual != value:
            raise Failure(
                "postcondition_failed",
                "The control did not retain the requested value.",
            )
        return {"applied": True, "value": actual}
    iface = value_iface(node)
    if iface is not None:
        try:
            number = float(value)
        except ValueError:
            raise Failure("invalid_request", "This control takes a number.")
        try:
            iface.set_current_value(number)
            actual = iface.get_current_value()
        except Exception as error:
            raise Failure(
                "action_failed",
                "AT-SPI value update failed: %s" % error,
            )
        if abs(actual - number) > 1e-6:
            raise Failure(
                "postcondition_failed",
                "The control did not retain the requested value.",
            )
        return {"applied": True, "value": value}
    raise Failure(
        "unsupported_operation",
        "The element does not support Value.",
    )


def op_idle_ms(args):
    return X.idle_ms()


def op_ping(args):
    desktop = Atspi.get_desktop(0)
    if desktop is None:
        raise Failure(
            "accessibility_permission_required",
            "The AT-SPI registry is not reachable.",
        )
    desktop.get_child_count()
    return {"ok": True}


HANDLERS = {
    "active": op_active,
    "click": op_click,
    "close": op_close,
    "drag": op_drag,
    "idle_ms": op_idle_ms,
    "invoke": op_invoke,
    "key": op_key,
    "observe": op_observe,
    "ping": op_ping,
    "scroll": op_scroll,
    "sequence": op_sequence,
    "set_value": op_set_value,
    "tree": op_tree,
    "type": op_type,
    "window": op_window,
    "windows": op_windows,
}


def log_call(path, op, started, reply):
    if not path:
        return
    entry = {
        "t": round(time.time(), 3),
        "op": op,
        "ms": int((time.time() - started) * 1000),
        "ok": reply.get("ok"),
    }
    if not reply.get("ok"):
        entry["code"] = reply["error"]["code"]
        entry["message"] = reply["error"]["message"][:200]
    try:
        with open(path, "a") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def main():
    global X
    log_path = os.environ.get("QWENPAW_CU_SIDECAR_LOG", "")
    startup_error = None
    try:
        Atspi.init()
        Gdk.init([])
        if Gdk.Display.get_default() is None:
            raise Failure(
                "accessibility_permission_required",
                "No X display is available.",
            )
        X = X11()
    except Failure as failure:
        startup_error = failure
    except Exception as error:
        startup_error = Failure(
            "accessibility_permission_required",
            str(error),
        )
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        started = time.time()
        request_id, op = None, ""
        try:
            request = json.loads(line)
            request_id = request.get("id")
            op = request.get("op") or ""
            if startup_error is not None:
                raise startup_error
            handler = HANDLERS.get(op)
            if handler is None:
                raise Failure(
                    "unsupported_operation",
                    "Unknown platform operation: %s" % op,
                )
            pump()
            reply = {
                "id": request_id,
                "ok": True,
                "result": handler(request.get("args") or {}),
            }
        except Failure as failure:
            reply = {
                "id": request_id,
                "ok": False,
                "error": {"code": failure.code, "message": failure.message},
            }
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            reply = {
                "id": request_id,
                "ok": False,
                "error": {"code": "action_failed", "message": str(error)},
            }
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
        log_call(log_path, op, started, reply)


if __name__ == "__main__":
    main()

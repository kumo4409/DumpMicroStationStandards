"""
dump_standards.py - Dump every CAD standard reachable from the active DGN
(seed) file in MicroStation 2025 (25.00.00.x) using the MicroStation Python
API (Python 3.12).

What it exports
---------------
* Session / file / model information (units, global origin, GCS, models)
* Levels (all definitions + symbology, flags, filters) and level usage
* Color table
* Line styles (file + system/resource), text styles, dimension styles,
  multiline styles, display styles, fonts, item types, element templates,
  named/saved views, view groups, reference attachments
* Cells: cell models + shared cell definitions in the file, plus every cell
  library found through MS_CELLLIST / MS_CELL / MS_CELLOUT
* DGN libraries (MS_DGNLIBLIST and every *_DGNLIBLIST*) - each one is opened
  read-only and its levels/styles/cells/templates are dumped too
* Configuration variables (all that can be discovered, fully expanded),
  the .cfg files that define them, and an analysis of every path/location

Output
------
A folder (see OUTPUT_DIR below) containing:
  standards.json          - everything, machine readable (primary output)
  levels.csv              - seed + DGN library levels, one row per level
  config_variables.csv    - name / expanded value
  paths.csv               - every path referenced by a config variable
  cells.csv               - every cell found (file + libraries)
  summary.txt             - human readable counts + any errors

How to run
----------
Open the seed file in MicroStation, then load/run this script from the
Python Manager (Utilities > Python Manager). Progress is printed to the
Python console and a message is posted to the Message Center.

Design note
-----------
The MicroStation Python bindings are a thin layer over the C++ DgnPlatform
API and some method names/signatures differ between builds. Every API call
here is therefore made through small "try several spellings" helpers, and
every section runs inside its own try/except. A failure in one section is
recorded in the "_errors" list of the JSON and never aborts the dump. In
addition each object is introspected generically (all zero-argument
Get*/Is*/Has* methods are called) so properties are captured even when the
explicit field list below does not know about them.
"""

from __future__ import annotations

import csv
import datetime as _dt
import enum
import faulthandler
import fnmatch
import getpass
import glob
import json
import math
import os
import platform
import re
import sys
import tempfile
import time
import traceback

# ---------------------------------------------------------------------------
# User settings
# ---------------------------------------------------------------------------

# Where to write the dump. Each run creates a "<seed name>_<timestamp>"
# subfolder here. Set to None to write next to the seed file instead.
# The environment variable STANDARDS_DUMP_DIR overrides this.
OUTPUT_DIR: str | None = (
    r"C:\Users\u12770\OneDrive - Metropolitan Water District of So Cal"
    r"\Documents\Standards_Dump"
)

# Open each DGN library and dump its standards (levels, styles, cells, ...).
DUMP_DGNLIBS = True
# Open each cell library and list its cells.
DUMP_CELL_LIBRARIES = True
# Count element usage (per level / color / style / weight / type) in the
# active model. Can be slow on very large models.
DUMP_ACTIVE_MODEL_USAGE = True
# Scan workspace .cfg files to discover every configuration variable name.
SCAN_CFG_FILES = True
# Call every zero-argument Get*/Is*/Has* method on objects. This captures
# more properties but calls native methods blindly; some of them can crash
# MicroStation. Leave off unless you need the extra "all" detail.
DEEP_INTROSPECTION = False
# Enumerate config variables through a native callback. Off by default:
# Python callbacks from native code are a crash risk. The .cfg scan finds
# the variable names without it.
USE_NATIVE_VARIABLE_ITERATION = False
# Sections to skip, e.g. {"active_model_usage", "dictionary"}. If a run
# crashes, crash.log / progress.log name the section; add it here.
SKIP_SECTIONS: set[str] = set()
# Read text/dimension style properties (height, font, arrow size, ...) by
# trying each property id with the style's typed getters.
PROBE_STYLE_PROPERTIES = True

MAX_LIBRARY_FILES = 400        # cap for dgnlibs + cell libraries opened
MAX_USAGE_ELEMENTS = 500_000   # cap for active-model usage scan
MAX_LIST_ITEMS = 500           # cap for any nested list inside introspection
MAX_CFG_FILES = 3000           # cap for .cfg files scanned
MAX_NAMES_PER_TYPE = 1000      # cap for names listed per dictionary type

SCRIPT_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# MicroStation imports
# ---------------------------------------------------------------------------

_IMPORT_ERRORS: list[str] = []
for _mod in ("MSPyBentley", "MSPyBentleyGeom", "MSPyECObjects",
             "MSPyDgnPlatform", "MSPyDgnView", "MSPyMstnPlatform"):
    try:
        exec(f"from {_mod} import *")
    except Exception as _e:  # pragma: no cover - only outside MicroStation
        _IMPORT_ERRORS.append(f"{_mod}: {_e}")

_G = globals()


def api(*names):
    """Return the first MicroStation API symbol that exists, else None."""
    for n in names:
        obj = _G.get(n)
        if obj is not None:
            return obj
    return None


# ---------------------------------------------------------------------------
# Logging / error capture
# ---------------------------------------------------------------------------

ERRORS: list[dict] = []
NOTES: list[str] = []
_T0 = time.time()


_PROGRESS_FH = None   # progress.log - survives a MicroStation crash
_CRASH_FH = None      # crash.log - faulthandler writes native crash tracebacks


def crumb(msg: str) -> None:
    """Write a breadcrumb to progress.log only (flushed to the OS)."""
    if _PROGRESS_FH is not None:
        try:
            _PROGRESS_FH.write(f"{time.time() - _T0:8.2f}s {msg}\n")
            _PROGRESS_FH.flush()
        except Exception:
            pass


def log(msg: str) -> None:
    print(f"[dump_standards {time.time() - _T0:7.1f}s] {msg}", flush=True)
    crumb(msg)


def start_crash_diagnostics(out_dir: str) -> None:
    global _PROGRESS_FH, _CRASH_FH
    try:
        _PROGRESS_FH = open(os.path.join(out_dir, "progress.log"), "w", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print("could not create progress.log:", e)
    try:
        _CRASH_FH = open(os.path.join(out_dir, "crash.log"), "w", encoding="utf-8")
        faulthandler.enable(file=_CRASH_FH, all_threads=True)
    except Exception as e:  # noqa: BLE001
        print("could not enable faulthandler:", e)


def stop_crash_diagnostics(out_dir: str) -> None:
    global _PROGRESS_FH, _CRASH_FH
    try:
        faulthandler.disable()
    except Exception:
        pass
    for fh in (_CRASH_FH, _PROGRESS_FH):
        try:
            if fh is not None:
                fh.close()
        except Exception:
            pass
    _PROGRESS_FH = _CRASH_FH = None
    try:
        cp = os.path.join(out_dir, "crash.log")
        if os.path.exists(cp) and os.path.getsize(cp) == 0:
            os.remove(cp)
    except Exception:
        pass


def record_error(where: str, exc: BaseException | str) -> None:
    if isinstance(exc, BaseException):
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        tb = traceback.format_exc(limit=4)
    else:
        detail, tb = str(exc), ""
    ERRORS.append({"where": where, "error": detail, "traceback": tb})
    log(f"WARNING {where}: {detail}")


def section(name: str, fn, *args, default=None, **kwargs):
    """Run one collector; never raise."""
    if name in SKIP_SECTIONS:
        log(f"skipping {name} (SKIP_SECTIONS)")
        return default if default is not None else {"_skipped": True}
    log(f"collecting {name} ...")
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        record_error(name, e)
        return default if default is not None else {"_error": str(e)}


# ---------------------------------------------------------------------------
# Generic helpers for the bindings
# ---------------------------------------------------------------------------

def is_ok(status) -> bool:
    """True for SUCCESS-style status values (0 / eSUCCESS / True)."""
    if status is None:
        return True
    if isinstance(status, bool):
        return status
    try:
        return int(status) == 0
    except Exception:
        name = getattr(status, "name", str(status))
        return "SUCCESS" in str(name).upper()


def _looks_like_status(v) -> bool:
    tn = type(v).__name__
    return "Status" in tn or tn in ("BentleyStatus", "StatusInt")


def unwrap(result):
    """Normalise binding results: (status, value), (value, status), value."""
    if isinstance(result, tuple):
        if len(result) == 0:
            return None
        if len(result) == 1:
            return result[0]
        if _looks_like_status(result[0]):
            return result[1] if is_ok(result[0]) else None
        if _looks_like_status(result[-1]):
            return result[0] if is_ok(result[-1]) else None
        return result[0]
    return result


def call(obj, *names, args=(), default=None):
    """Call the first method in `names` that exists on obj and succeeds."""
    if obj is None:
        return default
    for n in names:
        fn = getattr(obj, n, None)
        if fn is None:
            continue
        try:
            return fn(*args) if callable(fn) else fn
        except Exception:
            continue
    return default


def call_out(obj, name, *args):
    """Call a method that uses a WString out-parameter as first argument.

    Tries `obj.name(*args)` returning a value first, then
    `obj.name(WString(), *args)` and returns the WString.
    """
    fn = getattr(obj, name, None) if obj is not None else None
    if fn is None:
        return None
    try:
        r = unwrap(fn(*args))
        if isinstance(r, str) or type(r).__name__ in ("WString", "Utf8String", "AString"):
            return s(r) or None
    except Exception:
        pass
    WS = api("WString")
    if WS is not None:
        try:
            w = WS()
            st = fn(w, *args)
            st = st[0] if isinstance(st, tuple) and st else st
            if is_ok(st) and s(w):
                return s(w)
        except Exception:
            pass
    return None


def s(v) -> str | None:
    """String conversion that is safe for WString / Utf8String / None."""
    if v is None:
        return None
    try:
        if hasattr(v, "GetWCharCP"):
            return str(v.GetWCharCP())
    except Exception:
        pass
    try:
        return str(v)
    except Exception:
        return repr(v)


def iterate(obj, limit: int | None = None):
    """Iterate any binding collection safely."""
    if obj is None:
        return
    t = type(obj)
    if getattr(t, "__iter__", None) is None:
        # Python would otherwise call __getitem__(0, 1, 2, ...) until
        # IndexError; native containers may not bounds-check, so only index
        # up to an explicit length.
        n = None
        for ln in ("__len__", "size", "GetCount", "GetSize", "Count"):
            f = getattr(obj, ln, None)
            if f is None:
                continue
            try:
                n = int(f())
                break
            except Exception:
                continue
        if n is None or getattr(t, "__getitem__", None) is None:
            return
        for i in range(min(n, limit) if limit is not None else n):
            try:
                yield obj[i]
            except Exception:
                return
        return
    try:
        it = iter(obj)
    except Exception:
        return
    i = 0
    while True:
        if limit is not None and i >= limit:
            return
        try:
            x = next(it)
        except StopIteration:
            return
        except Exception as e:  # noqa: BLE001
            record_error(f"iterate {type(obj).__name__}", e)
            return
        i += 1
        yield x


_GETTER_PREFIXES = ("Get", "Is", "Has")
_GETTER_DENY_SUBSTR = ("Iterator", "Begin", "Next", "Previous", "Create",
                       "Copy", "Clone", "Delete", "Remove", "Release")
# Getters that return large/expensive objects - skipped during introspection
_GETTER_DENY_EXACT = {
    "GetDgnFile", "GetDgnFileP", "GetDgnFileR", "GetDgnModel", "GetDgnModelP",
    "GetDgnModelR", "GetModelRef", "GetDgnModelRef", "GetRoot", "GetRootP",
    "GetLevelCache", "GetDictionaryModel", "GetHandler", "GetElementRef",
    "GetElementRefP", "GetParentModelRef", "GetDgnAttachmentsP",
    "GetDgnAttachments", "GetReachableModelRefs", "GetDocument",
    "GetDocumentPtr", "GetModelIndex", "GetGraphicElements",
    "GetControlElements", "GetElementsCollection", "GetNamedViews",
    "GetViewGroups", "GetHost", "GetEditElementHandle", "GetElementCP",
    "GetElementP", "GetElementDescrCP", "GetElementDescrP", "GetXAttributes",
    "GetCache", "GetTransientCache", "GetProperties",
}
_TYPE_GETTERS: dict[type, list[str]] = {}
_TYPE_ARGREQ: dict[tuple, set[str]] = {}


def _getter_names(t: type) -> list[str]:
    names = _TYPE_GETTERS.get(t)
    if names is None:
        names = []
        for n in dir(t):
            if not n.startswith(_GETTER_PREFIXES):
                continue
            if n in _GETTER_DENY_EXACT or any(d in n for d in _GETTER_DENY_SUBSTR):
                continue
            names.append(n)
        _TYPE_GETTERS[t] = names
    return names


def _num(v):
    f = float(v)
    return f if math.isfinite(f) else str(f)


def _point_like(v):
    try:
        if all(hasattr(v, a) for a in ("red", "green", "blue")):
            return {"red": int(v.red), "green": int(v.green), "blue": int(v.blue)}
        if hasattr(v, "x") and hasattr(v, "y") and isinstance(v.x, (int, float)):
            d = {"x": _num(v.x), "y": _num(v.y)}
            if hasattr(v, "z") and isinstance(v.z, (int, float)):
                d["z"] = _num(v.z)
            return d
        if hasattr(v, "low") and hasattr(v, "high"):
            lo, hi = _point_like(v.low), _point_like(v.high)
            if lo is not None and hi is not None:
                return {"low": lo, "high": hi}
    except Exception:
        pass
    return None


def to_plain(v, depth: int = 0):
    """Convert binding values into JSON-able Python values."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, enum.Enum):
        return v.name
    t = type(v)
    if hasattr(t, "__members__") and hasattr(v, "name"):  # pybind11 enum
        try:
            return str(v.name)
        except Exception:
            pass
    tn = t.__name__
    if tn in ("WString", "Utf8String", "AString", "WStringRef"):
        return s(v)
    if isinstance(v, (bytes, bytearray)):
        return v[:256].hex()
    if isinstance(v, dict):
        return {str(k): to_plain(x, depth) for k, x in list(v.items())[:MAX_LIST_ITEMS]}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [to_plain(x, depth) for x in list(v)[:MAX_LIST_ITEMS]]
    pt = _point_like(v)
    if pt is not None:
        return pt
    if depth > 0:
        d = introspect(v, depth - 1)
        if d:
            return d
        # Iterable binding containers (arrays of ints, WStrings, ...)
        if hasattr(v, "__iter__") and hasattr(v, "__len__"):
            try:
                return [to_plain(x, depth - 1) for x in iterate(v, MAX_LIST_ITEMS)]
            except Exception:
                pass
    r = repr(v)
    if " object at 0x" in r:
        return f"<{tn}>"
    return r[:300]


def introspect(obj, depth: int = 1) -> dict:
    """Call every zero-arg Get*/Is*/Has* method + read plain attributes."""
    if obj is None or not DEEP_INTROSPECTION:
        return {}
    is_class = isinstance(obj, type)
    t = obj if is_class else type(obj)
    argreq = _TYPE_ARGREQ.setdefault((t, is_class), set())
    out: dict = {}
    for name in _getter_names(t):
        if name in argreq:
            continue
        try:
            m = getattr(obj, name)
        except Exception:
            continue
        if not callable(m):
            out[name] = to_plain(m, depth)
            continue
        try:
            val = m()
        except TypeError:
            argreq.add(name)  # needs arguments - remember per type
            continue
        except Exception:
            continue
        try:
            out[name] = to_plain(val, depth)
        except Exception:
            pass
    if is_class:
        return out
    # plain data members / read-only properties (lower-case, e.g. .red)
    for name in dir(t):
        if name.startswith("_") or name in out or name.startswith(_GETTER_PREFIXES):
            continue
        attr = getattr(t, name, None)
        if not isinstance(attr, property):
            continue
        try:
            val = getattr(obj, name)
        except Exception:
            continue
        if callable(val):
            continue
        try:
            out[name] = to_plain(val, 0)
        except Exception:
            pass
    return out


def enum_members(enum_cls) -> dict:
    """{name: member} for pybind11 or Python enums."""
    if enum_cls is None:
        return {}
    try:
        if isinstance(enum_cls, type) and issubclass(enum_cls, enum.Enum):
            return {m.name: m for m in enum_cls}
    except Exception:
        pass
    members = getattr(enum_cls, "__members__", None)
    if isinstance(members, dict):
        return dict(members)
    return {}


def enum_properties(obj, enum_cls, getter_names) -> dict:
    """Read every property of an enum-keyed property bag (text/dim styles)."""
    out = {}
    for pname, member in enum_members(enum_cls).items():
        for g in getter_names:
            fn = getattr(obj, g, None)
            if fn is None:
                continue
            val = None
            got = False
            for args in ((member,),):
                try:
                    r = fn(*args)
                except Exception:
                    continue
                # (status, value) / (value, status) / value
                if isinstance(r, tuple):
                    if len(r) >= 2 and _looks_like_status(r[0]):
                        if is_ok(r[0]):
                            val, got = r[1], True
                    elif len(r) >= 2 and _looks_like_status(r[-1]):
                        if is_ok(r[-1]):
                            val, got = r[0], True
                    else:
                        val, got = r, True
                elif _looks_like_status(r):
                    continue
                else:
                    val, got = r, True
                if got:
                    break
            if got:
                out[pname] = to_plain(val, 1)
                break
    return out


# ---------------------------------------------------------------------------
# Configuration variables
# ---------------------------------------------------------------------------

def cfg_get(name: str) -> str | None:
    CM = api("ConfigurationManager")
    if CM is not None:
        WS = api("WString")
        if WS is not None:
            try:
                w = WS()
                st = CM.GetVariable(w, name)
                if is_ok(st):
                    v = s(w)
                    return v
            except Exception:
                pass
        try:
            r = CM.GetVariable(name)
            r = unwrap(r)
            if r is not None and not _looks_like_status(r):
                return s(r)
        except Exception:
            pass
    return None


def cfg_defined(name: str) -> bool:
    CM = api("ConfigurationManager")
    try:
        return bool(CM.IsVariableDefined(name))
    except Exception:
        return cfg_get(name) is not None


def split_paths(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(";") if p.strip()]


KNOWN_CFG_VARS = """
MSDIR MS_DEF MS_DATA MS_TMP MS_BACKUP MS_SCR MS_EXE MS_CONFIG MS_LOCALDGNDIR
_DGNDIR _DGNFILE _ENGINENAME _VERSION_10_0 _USTN_PRODUCT_NAME _USTN_PRODUCT_VERSION
_USTN_INSTALLED_CONFIGURATION _USTN_CONFIGURATION _USTN_CUSTOM_CONFIGURATION
_USTN_WORKSPACESROOT _USTN_WORKSPACEROOT _USTN_WORKSPACENAME _USTN_WORKSPACECFG
_USTN_WORKSETSROOT _USTN_WORKSETROOT _USTN_WORKSETNAME _USTN_WORKSETCFG
_USTN_WORKSETSTANDARDS _USTN_WORKSETDATA _USTN_WORKSETDESCRIPTION
_USTN_WORKSPACESTANDARDS _USTN_ORGANIZATION _USTN_SYSTEM _USTN_ROLE _USTN_ROLENAME
_USTN_ROLECFG _USTN_HOMEROOT _USTN_HOMEPREFS _USTN_USERNAME _USTN_USER
_USTN_LOCALUSERAPPDATA _USTN_USERCFG _USTN_PROJECTNAME _USTN_PROJECT
_USTN_PROJECTDATA _USTN_DISCIPLINE _USTN_CAPABILITY _USTN_BENTLEYROOT
_USTN_APPL_CONFIGURATION _USTN_DGNLIBS
MS_DGNLIBLIST MS_DGNLIBLIST_DRAWINGSEEDS MS_DGNLIBLIST_PRINTING
MS_DGNLIBLIST_DISPLAYSTYLES MS_DGNLIBLIST_SHEETSEEDS MS_DGNLIBLIST_DETAILINGSYMBOLS
MS_DGNLIBLIST_DIMSTYLES MS_DGNLIBLIST_TEXTSTYLES MS_DGNLIBLIST_ITEMTYPES
MS_DGNLIBLIST_LEVELS MS_DGNLIBLIST_ELEMENTTEMPLATES MS_DGNLIBLIST_RENDERING
MS_GUIDGNLIBLIST MS_DGNAPPS MS_INITAPPS MS_MDLAPPS MS_MDL MS_ADDINPATH
MS_CELL MS_CELLLIST MS_CELLOUT MS_CELLSELECTORDIR MS_CELLSELECTOR
MS_SEEDFILES MS_DESIGNSEED MS_SEED MS_SHEETSEED MS_DRAWINGSEED
MS_DESIGNMODELSEED MS_SHEETMODELSEED MS_DRAWINGMODELSEED MS_TRANSEED
MS_DWGSEED MS_SEEDZONE MS_DEFAULT_SEEDFILE MS_SEEDFILE
MS_SYMBRSRC MS_LINESTYLEPATH MS_LINESTYLE MS_LEVEL_LIB_DIR MS_LEVEL_SYNC_SOURCE
MS_FONTPATH MS_FONTCONFIGFILE MS_SYMBRSRC MS_SHXFONTS MS_TTFONTS
MS_MATERIAL MS_MATERIALPATH MS_MATERIAL_PALETTE MS_PATTERN MS_BUMP MS_IMAGE
MS_IMAGEOUT MS_RENDERDATA MS_LIGHTING MS_RENDERLIBS
MS_PENTABLE MS_PLTCFG_PATH MS_PLTFILES MS_PLOTDLG_DEF_PENTABLE MS_PRINTERLIST
MS_PRINT_ORGANIZER MS_PRINTDEF_PATH MS_PLTR MS_DEFAULTPLTCFGFILE
MS_PRINT_DGNLIB MS_PDFEXPORT MS_PDF_OUTPUT
MS_RFDIR MS_REF_DEFAULTSETTINGS MS_REF_NEWLEVELDISPLAY MS_RASTER_SEARCHPATH
MS_GEOCOORDINATE_DATA MS_GEOCOORDINATE_USERLIBRARY MS_GEOCOORDINATE_LIBRARY
MS_DWGDATA MS_DWGFONTPATH MS_DWG_FONTMAP MS_DWGSETTINGSFILE MS_DXFSETTINGSFILE
MS_DWGLEVELMAPFILE MS_DWG_LEVELMAP MS_DWGLINESTYLEMAP MS_DWGLINEWEIGHTMAP
MS_DWG_FONTSUBST MS_DWGCOLORMAP
MS_STANDARDS_CHECKER_SETTINGS MS_STANDARDS_CHECKER_APPS MS_STANDARDS_CHECKER_OUT
MS_CUSTOMLINESTYLE MS_LEVELNAME_DGNLIB MS_TEXTSTYLE MS_DIMSTYLE
MS_ACCUDRAWMODE MS_DATABASE MS_DBEXT MS_SERVER MS_LINKTYPE
MS_SETTINGS MS_SETTINGSDIR MS_SETTINGSOUTDIR MS_PREFS MS_USERPREF MS_USERPREFDIR
MS_GUIHAND MS_INTERFACE MS_ICONPATH MS_HELPPATH MS_KEYINFILE MS_FUNCKEY MS_FKEYMNU
MS_UNITS MS_CUSTOMUNITDEF MS_ITEMTYPE_LIBRARY MS_ITEMTYPEPATH MS_REPORT_OUTPUT
MS_REPORTDEFINITIONS MS_SHEETINDEX MS_DRAWINGBOUNDARY MS_DETAILINGSYMBOLS
MS_ANNOTATIONSCALE MS_NAMEDEXPRESSIONS MS_VARIABLES MS_ELEMENTTEMPLATE
MS_DEFAULT_ELEMENT_TEMPLATE MS_DISPLAYSTYLE MS_PROTOTYPESEED
MS_PYTHONPATH MS_PYTHON MS_PYTHONHOME MS_PYTHONEXE MS_VBASEARCHDIRECTORIES
MS_VBAAUTOLOADPROJECTS MS_VBANEWPROJECTDIRECTORY MS_MACROS MS_MACRO
MS_TASKNAVIGATION_CONFIGURATION MS_WORKFLOW MS_RIBBONWORKFLOW
""".split()


def discover_cfg_var_names() -> tuple[set[str], list[str], str]:
    """Return (names, scanned cfg files, method used)."""
    names: set[str] = set(KNOWN_CFG_VARS)
    methods = []

    # 1) Native enumeration (if the bindings expose it)
    CM = api("ConfigurationManager")
    Delegate = api("IVariableIterationDelegate")
    if (USE_NATIVE_VARIABLE_ITERATION and CM is not None and Delegate is not None
            and hasattr(CM, "IterateThroughVariables")):
        try:
            class _Collector(Delegate):  # type: ignore[misc, valid-type]
                def __init__(self):
                    super().__init__()
                    self.found: list[str] = []

                def EachVariableCallback(self, *args):  # noqa: N802
                    if args:
                        self.found.append(s(args[0]))

            c = _Collector()
            CM.IterateThroughVariables(c)
            if c.found:
                names.update(n for n in c.found if n)
                methods.append(f"IterateThroughVariables({len(c.found)})")
        except Exception as e:  # noqa: BLE001
            NOTES.append(f"IterateThroughVariables unavailable: {e}")

    # 2) Environment variables can be config variables too
    names.update(k for k in os.environ.keys() if re.match(r"^[A-Za-z_]\w*$", k))

    # 3) Parse every .cfg/.ucf/.pcf file in the configuration tree
    scanned: list[str] = []
    if SCAN_CFG_FILES:
        roots = []
        for v in ("_USTN_CONFIGURATION", "_USTN_INSTALLED_CONFIGURATION",
                  "_USTN_CUSTOM_CONFIGURATION", "_USTN_WORKSPACESROOT",
                  "_USTN_WORKSPACEROOT", "_USTN_WORKSETSROOT", "_USTN_WORKSETROOT",
                  "_USTN_ORGANIZATION", "_USTN_SYSTEM", "_USTN_HOMEROOT",
                  "_USTN_HOMEPREFS", "_USTN_WORKSPACECFG", "_USTN_WORKSETCFG",
                  "_USTN_ROLECFG", "_USTN_USERCFG", "MSDIR"):
            for p in split_paths(cfg_get(v) or os.environ.get(v)):
                roots.append(p)
        msdir = cfg_get("MSDIR") or os.environ.get("MSDIR")
        if msdir:
            roots.append(os.path.join(msdir, "config"))
        seen_files: set[str] = set()
        def_re = re.compile(r"^\s*(?:%lock\s+|%undef\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:[=<>:]|$)")
        ref_re = re.compile(r"\$[({]([A-Za-z_][A-Za-z0-9_]*)[)}]")
        for root in roots:
            if len(scanned) >= MAX_CFG_FILES:
                break
            candidates = []
            if os.path.isfile(root):
                candidates = [root]
            elif os.path.isdir(root):
                for dp, dn, fns in os.walk(root):
                    # skip obviously heavy data folders
                    dn[:] = [d for d in dn if d.lower() not in ("cell", "cells", "dgn", "data", "out", "backup", "symb", "images", "textures")]
                    for fn in fns:
                        if fn.lower().endswith((".cfg", ".ucf", ".pcf", ".wscfg", ".wsetcfg")):
                            candidates.append(os.path.join(dp, fn))
                    if len(candidates) + len(scanned) > MAX_CFG_FILES:
                        break
            for f in candidates:
                key = os.path.normcase(os.path.abspath(f))
                if key in seen_files:
                    continue
                seen_files.add(key)
                scanned.append(f)
                try:
                    with open(f, "r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            line = line.split("#", 1)[0]
                            if not line.strip():
                                continue
                            m = def_re.match(line)
                            if m and not line.lstrip().startswith(("%if", "%elif", "%else", "%endif", "%include", "%error", "%level")):
                                names.add(m.group(1))
                            names.update(ref_re.findall(line))
                except Exception:
                    continue
        methods.append(f"cfg-scan({len(scanned)} files)")
    methods.append("known-list")
    return names, scanned, ", ".join(methods)


def collect_configuration() -> dict:
    names, scanned, method = discover_cfg_var_names()
    variables = {}
    for n in sorted(names, key=str.upper):
        v = cfg_get(n)
        if v is None:
            continue
        variables[n] = v
    # If the binding could not read anything fall back to environment
    if not variables:
        NOTES.append("ConfigurationManager.GetVariable returned nothing - using os.environ")
        variables = {k: v for k, v in os.environ.items()}
    return {
        "discovery_method": method,
        "variable_count": len(variables),
        "variables": variables,
        "cfg_files_scanned": scanned,
    }


def analyse_paths(variables: dict) -> list[dict]:
    rows = []
    for name, value in variables.items():
        for part in split_paths(value):
            if not (("\\" in part) or ("/" in part) or re.match(r"^[A-Za-z]:", part)):
                continue
            has_wild = any(c in part for c in "*?")
            entry = {"variable": name, "path": part,
                     "exists": False, "is_dir": False, "is_file": False,
                     "wildcard": has_wild, "matches": None}
            try:
                if has_wild:
                    m = glob.glob(part)
                    entry["matches"] = len(m)
                    entry["exists"] = bool(m)
                else:
                    entry["exists"] = os.path.exists(part)
                    entry["is_dir"] = os.path.isdir(part)
                    entry["is_file"] = os.path.isfile(part)
            except Exception:
                pass
            rows.append(entry)
    return rows


# ---------------------------------------------------------------------------
# Session / file access
# ---------------------------------------------------------------------------

def session_get(*names):
    SM = api("ISessionMgr")
    if SM is None:
        return None
    for n in names:
        try:
            a = getattr(SM, n, None)
        except Exception:
            a = None
        if a is None:
            continue
        try:
            v = a() if callable(a) else a
        except Exception:
            continue
        if v is not None:
            return v
    return None


def active_dgn_file():
    return session_get("GetActiveDgnFile", "ActiveDgnFile")


def active_model_ref():
    return session_get("GetActiveDgnModelRefP", "ActiveDgnModelRef", "GetActiveDgnModelRef")


def active_model():
    m = session_get("GetActiveDgnModelP", "ActiveDgnModel", "GetActiveDgnModel")
    if m is None:
        mr = active_model_ref()
        m = call(mr, "GetDgnModelP", "GetDgnModel")
    return m


def file_name(dgn_file) -> str | None:
    n = call(dgn_file, "GetFileName")
    if n is None:
        doc = call(dgn_file, "GetDocument", "GetDocumentPtr")
        n = call(doc, "GetFileName")
    return s(n)


def open_dgn_readonly(path: str):
    """Open a DGN/DGNLIB/CEL read-only. Returns DgnFile or raises."""
    DgnDocument = api("DgnDocument")
    DgnFile = api("DgnFile")
    OpenMode = api("DgnFileOpenMode")
    if DgnDocument is None or DgnFile is None:
        raise RuntimeError("DgnDocument/DgnFile not available")
    fetch = getattr(getattr(DgnDocument, "FetchMode", None), "eRead", None)
    doc = None
    attempts = []
    if fetch is not None:
        attempts += [(path, None, -101, fetch), (path, "", -101, fetch)]
    attempts += [(path,)]
    for a in attempts:
        try:
            doc = unwrap(DgnDocument.CreateFromFileName(*a))
        except Exception:
            doc = None
        if doc is not None and not _looks_like_status(doc):
            break
        doc = None
    if doc is None:
        doc = unwrap(call(DgnDocument, "CreateForLocalFile", args=(path,)))
    if doc is None:
        raise RuntimeError("could not create DgnDocument")
    mode = getattr(OpenMode, "eReadOnly", None) if OpenMode is not None else None
    f = DgnFile(doc, mode) if mode is not None else DgnFile(doc)
    st = f.LoadDgnFile()
    st0 = st[0] if isinstance(st, tuple) else st
    if not is_ok(st0):
        raise RuntimeError(f"LoadDgnFile failed: {st0}")
    for fill in ("FillDictionaryModel",):
        try:
            getattr(f, fill)()
        except Exception:
            pass
    return f


def model_index_items(dgn_file):
    """Yield model index entries. A generator, so the native index (and its
    iterator) stay alive while the caller reads each entry."""
    idx = call(dgn_file, "GetModelIndex")
    produced = False
    for item in iterate(idx):
        produced = True
        yield item
    if not produced:
        coll = call(dgn_file, "GetModelItemsList", "GetModelIndexCollection")
        for item in iterate(coll):
            yield item


def load_model(dgn_file, model_id):
    for fn in ("FindLoadedModelById", "LoadRootModelById", "LoadModelById"):
        f = getattr(dgn_file, fn, None)
        if f is None:
            continue
        for args in ((model_id,), (model_id, True), (model_id, True, False, True)):
            try:
                r = f(*args)
            except Exception:
                continue
            if isinstance(r, tuple):
                for x in r:
                    if x is not None and hasattr(x, "GetModelInfo"):
                        return x
            elif r is not None and hasattr(r, "GetModelInfo"):
                return r
    return None


def dictionary_model(dgn_file):
    return call(dgn_file, "GetDictionaryModel")


def model_elements(model, limit=None):
    """Yield ElementRefs from a model (control + graphic)."""
    if model is None:
        return
    produced = False
    count = 0
    for getter in ("GetControlElementsP", "GetControlElements",
                   "GetGraphicElementsP", "GetGraphicElements"):
        coll = call(model, getter)
        if coll is None:
            continue
        # avoid double-iterating the P / non-P twin of the same list
        if getter.endswith("P") and call(model, getter[:-1]) is not None:
            continue
        for r in iterate(coll):
            produced = True
            yield r
            count += 1
            if limit and count >= limit:
                return
    if not produced:
        for r in iterate(call(model, "GetElementsCollection")):
            yield r
            count += 1
            if limit and count >= limit:
                return


def element_handle(ref, model=None):
    EH = api("ElementHandle")
    if EH is None:
        return None
    for args in ((ref,), (ref, model)):
        try:
            eh = EH(*args)
            if call(eh, "IsValid", default=True):
                return eh
        except Exception:
            continue
    return None


ELEMENT_TYPE_NAMES = {
    1: "Cell Library Header", 2: "Cell Header", 3: "Line", 4: "Line String",
    5: "Group Data", 6: "Shape", 7: "Text Node", 8: "Digitizer Setup",
    9: "Design File Header", 10: "Level Symbology", 11: "Curve", 12: "Complex String",
    14: "Complex Shape", 15: "Ellipse", 16: "Arc", 17: "Text", 18: "3D Surface",
    19: "3D Solid", 21: "B-spline Pole", 22: "Point String", 23: "Cone",
    24: "B-spline Surface", 25: "B-spline Surface Boundary", 26: "B-spline Knot",
    27: "B-spline Curve", 28: "B-spline Weight Factor", 33: "Dimension",
    34: "Shared Cell Definition", 35: "Shared Cell Instance", 36: "Multiline",
    37: "Attribute", 38: "DgnStore Component", 39: "DgnStore Header",
    66: "MicroStation Application (Type 66)", 87: "Raster Reference",
    88: "Raster Reference Attribute", 89: "Raster Hierarchy Header",
    90: "Raster Hierarchy Component", 94: "Raster Frame", 95: "Table Entry",
    96: "Table Header", 97: "View Group", 98: "View", 99: "Level Mask",
    100: "Reference Attachment", 101: "Matrix Header", 102: "Matrix Int Data",
    103: "Matrix Double Data", 105: "Mesh Header", 106: "Extended Element",
    107: "Extended Non-graphic", 108: "Reference Override", 110: "Named Group",
    112: "Text Node (V8)",
}


def element_info(eh) -> dict:
    t = call(eh, "GetElementType")
    try:
        t = int(t) if t is not None else None
    except Exception:
        pass
    d = {"type": t, "type_name": ELEMENT_TYPE_NAMES.get(t, str(t)),
         "id": call(eh, "GetElementId")}
    h = call(eh, "GetHandler")
    if h is not None:
        desc = None
        WS = api("WString")
        if WS is not None:
            try:
                w = WS()
                h.GetDescription(eh, w, 256)
                desc = s(w)
            except Exception:
                desc = None
        if not desc:
            for args in ((eh, 256), (eh,)):
                try:
                    r = unwrap(h.GetDescription(*args))
                except Exception:
                    continue
                if isinstance(r, str) or type(r).__name__ in ("WString", "Utf8String"):
                    desc = s(r)
                    break
        if desc:
            d["description"] = desc
    return d


def cell_name_of(eh) -> tuple[str | None, str | None]:
    """Extract (name, description) from a cell / shared cell element."""
    name = desc = None
    h = call(eh, "GetHandler")
    for target in (h, api("CellUtil"), api("ICellQuery")):
        if target is None:
            continue
        if name is None:
            name = call_out(target, "ExtractName", eh) or call_out(target, "GetCellName", eh)
        if desc is None:
            desc = call_out(target, "ExtractDescription", eh)
        if name:
            break
    return name, desc


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def collect_environment(dgn_file) -> dict:
    keys = ["_USTN_PRODUCT_NAME", "_USTN_PRODUCT_VERSION", "_ENGINENAME",
            "_VERSION_10_0", "MSDIR", "_USTN_WORKSPACENAME", "_USTN_WORKSETNAME",
            "_USTN_ROLENAME", "_USTN_ORGANIZATION", "_USTN_CONFIGURATION"]
    return {
        "script_version": SCRIPT_VERSION,
        "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
        "python_version": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "computer": platform.node(),
        "user": _safe(getpass.getuser),
        "active_file": file_name(dgn_file),
        "active_model": s(call(active_model(), "GetModelName")),
        "microstation": {k: cfg_get(k) for k in keys},
        "import_errors": _IMPORT_ERRORS,
    }


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


def collect_file_info(dgn_file) -> dict:
    fn = file_name(dgn_file)
    info = {"file_name": fn}
    if fn and os.path.exists(fn):
        st = os.stat(fn)
        info.update(size_bytes=st.st_size,
                    modified=_dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"))
    info["properties"] = introspect(dgn_file, 1)
    return info


def unit_record(u) -> dict | None:
    if u is None:
        return None
    d = {
        "label": s(call(u, "GetLabel")),
        "numerator": call(u, "GetNumerator"),
        "denominator": call(u, "GetDenominator"),
        "base": to_plain(call(u, "GetBase")),
        "system": to_plain(call(u, "GetSystem")),
    }
    d["all"] = introspect(u, 0)
    return d


def model_info_record(model) -> dict:
    mi = call(model, "GetModelInfo", "GetModelInfoCP")
    d = {
        "name": s(call(model, "GetModelName")),
        "id": call(model, "GetModelId"),
        "type": to_plain(call(model, "GetModelType")),
        "is_3d": call(model, "Is3d", "Is3D"),
    }
    if mi is not None:
        d["master_unit"] = unit_record(call(mi, "GetMasterUnit"))
        d["sub_unit"] = unit_record(call(mi, "GetSubUnit"))
        d["storage_unit"] = unit_record(call(mi, "GetStorageUnit"))
        d["uor_per_storage"] = call(mi, "GetUorPerStorage")
        d["uor_per_meter"] = call(mi, "GetUorPerMeter")
        d["global_origin"] = to_plain(call(mi, "GetGlobalOrigin"))
        d["annotation_scale"] = call(mi, "GetAnnotationScaleFactor")
        d["linear_units_format"] = to_plain(call(mi, "GetLinearUnitMode"))
        d["linear_precision"] = to_plain(call(mi, "GetLinearPrecision"))
        d["angular_mode"] = to_plain(call(mi, "GetAngularMode"))
        d["angular_precision"] = to_plain(call(mi, "GetAngularPrecision"))
        d["direction_mode"] = to_plain(call(mi, "GetDirectionMode"))
        d["model_info_all"] = introspect(mi, 1)
    return d


def _is_cell_model(rec: dict) -> bool:
    """Best-effort detection of a cell model from a model index record."""
    def truthy(v):
        if v in (None, False, 0, ""):
            return False
        sv = str(v).lower()
        return not any(t in sv for t in ("none", "false", "normal", "notcell"))
    if truthy(rec.get("cell_type")):
        return True
    for k, v in (rec.get("all") or {}).items():
        if "cell" in k.lower() and not isinstance(v, (dict, list)) and truthy(v):
            return True
    return False


def collect_models(dgn_file, load_each: bool) -> list[dict]:
    out = []
    for item in model_index_items(dgn_file):
        rec = {
            "name": s(call(item, "GetName")),
            "description": s(call(item, "GetDescription")),
            "id": call(item, "GetModelId"),
            "type": to_plain(call(item, "GetModelType")),
            "cell_type": to_plain(call(item, "GetCellType")),
            "is_3d": call(item, "Is3D", "Is3d"),
            "is_hidden": call(item, "IsHidden"),
            "is_cell": None,
            "all": introspect(item, 1),
        }
        rec["is_cell"] = _is_cell_model(rec)
        out.append(rec)
    # Load models only after the index iteration has finished: loading can
    # change the index and invalidate a live iterator.
    if load_each:
        for rec in out:
            if rec["id"] is None:
                continue
            crumb(f"  loading model {rec['name']}")
            m = load_model(dgn_file, rec["id"])
            if m is not None:
                try:
                    rec["model_info"] = model_info_record(m)
                except Exception as e:  # noqa: BLE001
                    rec["model_info"] = {"_error": str(e)}
    return out


# --- colors ----------------------------------------------------------------

def color_table(dgn_file) -> list[dict]:
    CM = api("DgnColorMap")
    cmap = call(CM, "GetForFile", args=(dgn_file,)) if CM is not None else None
    if cmap is None:
        cmap = call(CM, "GetForDisplay", args=(dgn_file,)) if CM is not None else None
    rows = []
    tbgr = call(cmap, "GetTbgrColors")
    if tbgr is not None:
        for i, v in enumerate(iterate(tbgr, 256)):
            v = int(v)
            rows.append({"index": i, "red": v & 0xFF, "green": (v >> 8) & 0xFF, "blue": (v >> 16) & 0xFF})
        if rows:
            return rows
    for i in range(256):
        c = call(cmap, "GetColor", args=(i,))
        p = _point_like(c) if c is not None else None
        if p is None and c is not None:
            try:
                v = int(c)
                p = {"red": v & 0xFF, "green": (v >> 8) & 0xFF, "blue": (v >> 16) & 0xFF}
            except Exception:
                p = {"raw": to_plain(c)}
        if p is not None:
            rows.append({"index": i, **p})
    return rows


# --- line styles -----------------------------------------------------------

def line_style_name(num, dgn_file, cache: dict) -> str | None:
    if num is None:
        return None
    try:
        num = int(num)
    except Exception:
        return s(num)
    if num in cache:
        return cache[num]
    if 0 <= num <= 7:
        cache[num] = str(num)
        return cache[num]
    name = None
    LSM = api("LineStyleManager")
    if LSM is not None:
        name = call_out(LSM, "GetNameFromNumber", num, dgn_file)
    cache[num] = name
    return name


def _ls_entry(e) -> dict:
    d = {
        "name": s(call(e, "GetStyleName", "GetName")),
        "number": call(e, "GetStyleNumber", "GetNumber"),
        "all": introspect(e, 1),
    }
    comp = call(e, "GetComponent", "GetComponentCP")
    if comp is not None:
        d["component"] = {"type": to_plain(call(comp, "GetElementType", "GetComponentType")),
                          "description": s(call(comp, "GetDescription")),
                          "all": introspect(comp, 0)}
    return d


def collect_line_styles(dgn_file) -> dict:
    out: dict = {"file": [], "system": [], "resource_files": []}
    maps = []
    LsMap = api("LsMap")
    LsCache = api("LsCache")
    LsDgnFileMap = api("LsDgnFileMap")
    LsSystemMap = api("LsSystemMap")
    LSM = api("LineStyleManager")
    for owner, fn, args in ((LsMap, "GetMapPtr", (dgn_file, True)),
                            (LsMap, "GetMapPtr", (dgn_file,)),
                            (LsDgnFileMap, "GetMapPtr", (dgn_file, True)),
                            (LsCache, "GetDgnFileCache", (dgn_file,)),
                            (LSM, "GetNameMap", (dgn_file,)),
                            (dgn_file, "GetLineStyleMap", ())):
        if owner is None:
            continue
        m = unwrap(call(owner, fn, args=args))
        if m is not None:
            maps.append(("file", m))
            break
    for owner, fn, args in ((LsSystemMap, "GetSystemMapR", ()),
                            (LsSystemMap, "GetSystemMapP", (True,)),
                            (LsSystemMap, "GetSystemMap", ()),
                            (LsMap, "GetSystemMapP", (True,)),
                            (LSM, "GetSystemMap", ())):
        if owner is None:
            continue
        m = unwrap(call(owner, fn, args=args))
        if m is not None:
            maps.append(("system", m))
            break
    for kind, m in maps:
        for e in iterate(m):
            out[kind].append(_ls_entry(e))
        if not out[kind]:
            out[kind + "_map_properties"] = introspect(m, 1)
    if not maps:
        NOTES.append("Line style maps not exposed by bindings; only names referenced by levels/elements resolved.")
    out["resource_files"] = split_paths(cfg_get("MS_SYMBRSRC"))
    out["linestyle_dgnlib_vars"] = {k: cfg_get(k) for k in ("MS_LINESTYLEPATH", "MS_DGNLIBLIST")}
    return out


# --- levels ----------------------------------------------------------------

def _symb(obj, value_getters):
    if obj is None:
        return None
    if isinstance(obj, (int, float, str)):
        return obj
    for g in value_getters:
        v = call(obj, g)
        if v is not None:
            return to_plain(v, 1)
    return to_plain(obj, 1)


def level_record(lh, dgn_file, colors: list[dict], ls_cache: dict) -> dict:
    rec = {
        "name": s(call(lh, "GetName")),
        "display_name": s(call(lh, "GetDisplayName")),
        "id": call(lh, "GetLevelId"),
        "code": call(lh, "GetLevelCode"),
        "description": s(call(lh, "GetDescription")),
        "parent_id": call(lh, "GetParentLevelId"),
        "is_valid": call(lh, "IsValid"),
    }
    # By-level symbology
    bl_color = call(lh, "GetByLevelColor")
    rec["color"] = _symb(bl_color, ("GetColor",))
    rec["color_is_external"] = call(bl_color, "IsExternal", "IsExtended")
    bl_style = call(lh, "GetByLevelLineStyle", "GetByLevelStyle")
    rec["line_style"] = _symb(bl_style, ("GetStyle",))
    rec["line_style_params"] = to_plain(call(bl_style, "GetStyleParams", "GetStyleParamsCP"), 1)
    rec["weight"] = _symb(call(lh, "GetByLevelWeight"), ("GetWeight",))
    rec["material_id"] = to_plain(call(call(lh, "GetByLevelMaterial"), "GetId"), 0)
    rec["fill_color"] = _symb(call(lh, "GetByLevelFillColor"), ("GetColor",))
    # Override symbology
    rec["override_color"] = _symb(call(lh, "GetOverrideColor"), ("GetColor",))
    rec["override_line_style"] = _symb(call(lh, "GetOverrideLineStyle", "GetOverrideStyle"), ("GetStyle",))
    rec["override_weight"] = _symb(call(lh, "GetOverrideWeight"), ("GetWeight",))
    # Flags
    rec["display"] = call(lh, "GetDisplay", "IsDisplayed")
    rec["frozen"] = call(lh, "GetFrozen", "IsFrozen")
    rec["locked"] = call(lh, "GetLocked", "IsLocked")
    rec["plot"] = call(lh, "GetPlot", "IsPlot")
    rec["transparency"] = call(lh, "GetTransparency")
    rec["display_priority"] = call(lh, "GetDisplayPriority")
    rec["element_access"] = to_plain(call(lh, "GetElementAccess"))
    rec["is_library_level"] = call(lh, "IsFromLibrary", "GetIsFromLibrary")
    rec["library_name"] = s(call(lh, "GetLibraryName"))
    # Derived friendly values
    try:
        ci = int(rec["color"]) if rec["color"] is not None else None
    except Exception:
        ci = None
    if ci is not None and 0 <= ci < len(colors):
        c = colors[ci]
        rec["color_rgb"] = [c.get("red"), c.get("green"), c.get("blue")]
    elif ci is not None and ci > 255:
        rec["color_rgb"] = "extended/color-book"
    rec["line_style_name"] = line_style_name(rec["line_style"], dgn_file, ls_cache) \
        if isinstance(rec["line_style"], (int, float)) else None
    rec["all"] = introspect(lh, 1)
    return rec


def collect_levels(dgn_file, colors=None) -> dict:
    cache = call(dgn_file, "GetLevelCache")
    if cache is None:
        raise RuntimeError("GetLevelCache not available")
    colors = colors if colors is not None else []
    ls_cache: dict = {}
    levels = []
    for lh in iterate(cache):
        crumb(f"  level {s(call(lh, 'GetName'))}")
        try:
            levels.append(level_record(lh, dgn_file, colors, ls_cache))
        except Exception as e:  # noqa: BLE001
            record_error("level", e)
    # parent names for hierarchy
    by_id = {l["id"]: l["name"] for l in levels if l.get("id") is not None}
    for l in levels:
        pid = l.get("parent_id")
        if pid not in (None, 0, -1, 0xFFFFFFFF, 0xFFFFFFFE):
            l["parent_name"] = by_id.get(pid)
    filters = []
    for getter in ("GetLevelFilters", "GetFilters"):
        for f in iterate(call(cache, getter)):
            filters.append(introspect(f, 1))
        if filters:
            break
    LFC = api("LevelFilterCollection")
    if not filters and LFC is not None:
        try:
            for f in iterate(LFC(dgn_file)):
                filters.append(introspect(f, 1))
        except Exception:
            pass
    return {
        "count": len(levels),
        "cache_properties": introspect(cache, 0),
        "levels": sorted(levels, key=lambda l: (l.get("name") or "").upper()),
        "level_filters": filters,
    }


# --- styles -----------------------------------------------------------------

def _visit_collection(dgn_file, class_names, static_getters, visit):
    """Find a file's collection of styles/libraries and run visit(entry) on
    each entry *during* iteration.

    Entries handed out by a native collection may point into it, so they
    must not be kept after the collection or its iterator goes away. (Doing
    so crashed MicroStation with a pure-virtual-call abort.) Only plain
    Python data returned by visit() leaves this function.
    """
    def run(coll, src):
        results = []
        for entry in iterate(coll):
            try:
                results.append(visit(unwrap(entry)))
            except Exception as e:  # noqa: BLE001
                record_error(f"{src} entry", e)
        return results

    for cn in class_names:
        C = api(cn)
        if C is None:
            continue
        try:
            coll = C(dgn_file)
        except Exception:
            continue
        results = run(coll, cn)
        if results:
            return results, cn
    for owner_name, method in static_getters:
        owner = api(owner_name)
        if owner is None or getattr(owner, method, None) is None:
            continue
        coll = unwrap(call(owner, method, args=(dgn_file,)))
        if coll is None:
            continue
        results = run(coll, f"{owner_name}.{method}")
        if results:
            return results, f"{owner_name}.{method}"
    return [], None


def _owned_style(class_name, name, dgn_file):
    """Look a style up by name to get an object the caller owns."""
    C = api(class_name)
    if C is None or not name:
        return None
    return unwrap(call(C, "GetByName", args=(name, dgn_file)))


def collect_text_styles(dgn_file) -> dict:
    prop_enum = api("TextStyleProperty", "DgnTextStyleProperty")
    getters = ("GetProperty", "GetPropertyValue", "GetDoubleProperty", "GetIntegerProperty",
               "GetUInt32Property", "GetBoolProperty", "GetStringProperty",
               "GetColorProperty", "GetFontProperty", "GetDPoint2dProperty")

    def visit(st):
        name = s(call(st, "GetName"))
        crumb(f"  text style {name}")
        rec = {"name": name, "id": call(st, "GetID", "GetId")}
        own = _owned_style("DgnTextStyle", name, dgn_file)
        target = own if own is not None else st
        if PROBE_STYLE_PROPERTIES:
            rec["properties"] = enum_properties(target, prop_enum, getters)
        rec["all"] = introspect(target, 1)
        return rec

    styles, src = _visit_collection(
        dgn_file, ("TextStyleCollection", "DgnTextStyleCollection"),
        (("DgnTextStyle", "GetFileStyles"), ("DgnTextStyle", "BuildList"),
         ("DgnTextStyle", "GetDgnFileStyles"), ("TextStyleManager", "GetTextStyles")),
        visit)
    return {"source": src, "count": len(styles), "styles": styles}


def collect_dimension_styles(dgn_file) -> dict:
    prop_enum = api("DimStyleProp")
    getters = ("GetDoubleProp", "GetIntegerProp", "GetBooleanProp", "GetStringProp",
               "GetAccuracyProp", "GetLevelProp", "GetColorProp", "GetWeightProp",
               "GetLineStyleProp", "GetFontProp", "GetTemplateFlagProp", "GetUnitsProp",
               "GetCharProp", "GetOneUnitProp", "GetUnitLabelProp", "GetProp")

    def visit(st):
        name = s(call(st, "GetName"))
        crumb(f"  dimension style {name}")
        rec = {"name": name, "id": call(st, "GetID", "GetId"),
               "description": s(call(st, "GetDescription"))}
        own = _owned_style("DimensionStyle", name, dgn_file)
        target = own if own is not None else st
        if PROBE_STYLE_PROPERTIES:
            rec["properties"] = enum_properties(target, prop_enum, getters)
        rec["all"] = introspect(target, 1)
        return rec

    styles, src = _visit_collection(
        dgn_file, ("DimStyleCollection", "DimensionStyleCollection"),
        (("DimensionStyle", "GetFileStyles"), ("DimensionStyle", "BuildList")),
        visit)
    return {"source": src, "count": len(styles), "styles": styles}


def collect_multiline_styles(dgn_file) -> dict:
    def visit(st):
        name = s(call(st, "GetName"))
        crumb(f"  multiline style {name}")
        own = _owned_style("MultilineStyle", name, dgn_file)
        target = own if own is not None else st
        profiles = []
        n = call(target, "GetProfileLineCount", "GetProfileCount")
        if isinstance(n, int):
            for i in range(n):
                p = call(target, "GetProfile", "GetProfileCP", args=(i,))
                if p is not None:
                    profiles.append(introspect(p, 1) or to_plain(p, 0))
        return {"name": name, "id": call(st, "GetID", "GetId"),
                "profiles": profiles, "all": introspect(target, 1)}

    styles, src = _visit_collection(
        dgn_file, ("MultilineStyleCollection",), (("MultilineStyle", "GetFileStyles"),), visit)
    return {"source": src, "count": len(styles), "styles": styles}


def collect_display_styles(dgn_file) -> dict:
    def visit(d):
        name = s(call(d, "GetName"))
        crumb(f"  display style {name}")
        return {"name": name, "index": call(d, "GetIndex"), "all": introspect(d, 1)}

    styles: list = []
    src = None
    DSL = api("DisplayStyleList")
    DSLO = api("DisplayStyleListOptions")
    if DSL is not None:
        opts = list(enum_members(DSLO).values()) if DSLO is not None else []
        for args in [(dgn_file, o) for o in opts] + [(dgn_file,)]:
            try:
                coll = DSL(*args)
            except Exception:
                continue
            styles = [visit(unwrap(d)) for d in iterate(coll)]
            if styles:
                src = "DisplayStyleList"
                break
    if not styles:
        styles, src = _visit_collection(
            dgn_file, (),
            (("DisplayStyleManager", "GetDisplayStylesForFile"),
             ("DisplayStyleManager", "GetDisplayStyles"),
             ("DisplayStyleManager", "GetDisplayStylesInFile")),
            visit)
    return {"source": src, "count": len(styles), "styles": styles}


def collect_fonts(dgn_file) -> dict:
    out: dict = {"fonts": [], "paths": {k: cfg_get(k) for k in ("MS_FONTPATH", "MS_FONTCONFIGFILE", "MS_SYMBRSRC")}}
    FM = api("DgnFontManager")
    seen = set()
    for m, args in (("GetFontsForFile", (dgn_file,)), ("GetDgnFontMapP", (dgn_file,)),
                    ("GetDgnFontMap", (dgn_file,)), ("GetSystemFonts", ()),
                    ("CreateKnownFontList", ())):
        r = unwrap(call(FM, m, args=args))
        for f in iterate(r):
            f = unwrap(f)
            if isinstance(f, tuple) and len(f) == 2:
                num, f = f
            else:
                num = None
            name = s(call(f, "GetName"))
            key = (name, s(call(f, "GetType")))
            if key in seen:
                continue
            seen.add(key)
            out["fonts"].append({"source": m, "number": num, "name": name,
                                 "type": to_plain(call(f, "GetType")), "all": introspect(f, 0)})
    return out


def collect_item_types(dgn_file) -> dict:
    def visit(lib):
        name = s(call(lib, "GetName"))
        crumb(f"  item type library {name}")
        types = []
        for it in iterate(lib):
            props = []
            for p in iterate(it):
                props.append({"name": s(call(p, "GetName")), "label": s(call(p, "GetDisplayLabel", "GetLabel")),
                              "type": to_plain(call(p, "GetType")), "all": introspect(p, 0)})
            types.append({"name": s(call(it, "GetName")), "label": s(call(it, "GetDisplayLabel", "GetLabel")),
                          "properties": props, "all": introspect(it, 0)})
        return {"name": name, "label": s(call(lib, "GetDisplayLabel", "GetLabel")),
                "item_types": types, "all": introspect(lib, 0)}

    libs, src = _visit_collection(
        dgn_file, ("ItemTypeLibraries",),
        (("ItemTypeLibrary", "GetLibrariesInFile"), ("ItemTypeLibrary", "PopulateListFromFile")),
        visit)
    return {"source": src, "count": len(libs), "libraries": libs}


def collect_element_templates(dgn_file) -> dict:
    def walk(node, path):
        node = unwrap(node)
        name = s(call(node, "GetName"))
        full = f"{path}/{name}" if path else name
        crumb(f"  element template {full}")
        rec = {"path": full, "name": name, "is_group": call(node, "IsGroup", "IsGroupNode"),
               "all": introspect(node, 1)}
        res = [rec]
        children = call(node, "GetChildren", "GetChildNodes")
        for child in iterate(children):
            res.extend(walk(child, full))
        return res

    templates: list = []
    src = None
    for cls, m in (("ElementTemplateNode", "GetAllTemplatesInFile"),
                   ("ElementTemplateNode", "GetTemplatesInFile"),
                   ("ElementTemplateUtils", "GetAllTemplates"),
                   ("ETemplateCollection", None),
                   ("ElementTemplateNodeCollection", None)):
        C = api(cls)
        if C is None or (m is not None and getattr(C, m, None) is None):
            continue
        try:
            coll = C(dgn_file) if m is None else unwrap(getattr(C, m)(dgn_file))
        except Exception:
            continue
        for n in iterate(coll):
            templates.extend(walk(n, ""))
        if templates:
            src = f"{cls}.{m}" if m else cls
            break
    if not templates:
        NOTES.append("Element template API not exposed; see dictionary_inventory for raw entries.")
    return {"source": src, "count": len(templates), "templates": templates}


def collect_named_views(dgn_file) -> dict:
    views = []
    for v in iterate(call(dgn_file, "GetNamedViews")):
        v = unwrap(v)
        views.append({"name": s(call(v, "GetName")), "description": s(call(v, "GetDescription")),
                      "all": introspect(v, 1)})
    groups = []
    for g in iterate(call(dgn_file, "GetViewGroups")):
        g = unwrap(g)
        groups.append({"name": s(call(g, "GetName")), "description": s(call(g, "GetDescription")),
                       "all": introspect(g, 0)})
    return {"saved_views": views, "view_groups": groups}


def collect_references(model_ref) -> list[dict]:
    refs = []
    coll = call(model_ref, "GetDgnAttachmentsP", "GetDgnAttachments")
    for a in iterate(coll):
        refs.append({
            "logical_name": s(call(a, "GetLogicalName")),
            "file": s(call(a, "GetAttachFileName")),
            "full_path": s(call(a, "GetAttachFullFileSpec")),
            "model": s(call(a, "GetAttachModelName")),
            "description": s(call(a, "GetDescription")),
            "all": introspect(a, 1),
        })
    return refs


def collect_gcs(model) -> dict | None:
    G = api("DgnGCS")
    if G is None:
        return None
    gcs = unwrap(call(G, "FromModel", args=(model, True))) or unwrap(call(G, "FromModel", args=(model,)))
    if gcs is None:
        return {"assigned": False}
    return {"assigned": True, "name": s(call(gcs, "GetName")),
            "description": s(call(gcs, "GetDescription")),
            "all": introspect(gcs, 0)}


def collect_active_settings() -> dict:
    # Only static getters via introspection. ActiveParams.GetValue is not
    # called: it writes into a caller-typed buffer and is unsafe to probe.
    out = {}
    for cls in ("Settings", "ActiveSettings"):
        C = api(cls)
        if C is not None:
            out[cls] = introspect(C, 0)
    if not out:
        NOTES.append("Active settings need DEEP_INTROSPECTION = True")
    return out


# --- cells / dictionary ----------------------------------------------------

def collect_dictionary(dgn_file) -> dict:
    """Inventory of the non-model (dictionary) data: shared cells, templates..."""
    model = dictionary_model(dgn_file)
    if model is None:
        raise RuntimeError("GetDictionaryModel not available")
    counts: dict[str, int] = {}
    names: dict[str, list] = {}
    shared_cells = []
    for ref in model_elements(model):
        eh = element_handle(ref, model)
        if eh is None:
            continue
        info = element_info(eh)
        key = f"{info['type']}:{info['type_name']}"
        counts[key] = counts.get(key, 0) + 1
        if info["type"] == 34:
            nm, desc = cell_name_of(eh)
            shared_cells.append({"name": nm, "description": desc,
                                 "element_id": info.get("id"),
                                 "handler_description": info.get("description")})
        elif info.get("description"):
            lst = names.setdefault(key, [])
            if len(lst) < MAX_NAMES_PER_TYPE:
                lst.append(info["description"])
    return {"element_counts": counts, "descriptions": names, "shared_cell_definitions": shared_cells}


def collect_active_usage(model, levels: list[dict], dgn_file) -> dict:
    by_id = {l.get("id"): l.get("name") for l in levels}
    EPG = api("ElementPropertiesGetter")
    lev: dict = {}
    col: dict = {}
    sty: dict = {}
    wt: dict = {}
    typ: dict = {}
    cells: dict = {}
    n = 0
    ls_cache: dict = {}
    for ref in model_elements(model, MAX_USAGE_ELEMENTS):
        eh = element_handle(ref, model)
        if eh is None:
            continue
        n += 1
        t = call(eh, "GetElementType")
        try:
            t = int(t)
        except Exception:
            pass
        typ[ELEMENT_TYPE_NAMES.get(t, str(t))] = typ.get(ELEMENT_TYPE_NAMES.get(t, str(t)), 0) + 1
        g = None
        if EPG is not None:
            try:
                g = EPG(eh)
            except Exception:
                g = None
        if g is not None:
            lid = call(g, "GetLevel")
            ln = by_id.get(lid, str(lid))
            lev[ln] = lev.get(ln, 0) + 1
            c = call(g, "GetColor")
            col[str(c)] = col.get(str(c), 0) + 1
            st = call(g, "GetLineStyle")
            sn = line_style_name(st, dgn_file, ls_cache) or str(st)
            sty[sn] = sty.get(sn, 0) + 1
            w = call(g, "GetWeight")
            wt[str(w)] = wt.get(str(w), 0) + 1
        if t in (2, 35):
            nm, _ = cell_name_of(eh)
            if nm:
                cells[nm] = cells.get(nm, 0) + 1
    return {"elements_scanned": n, "truncated": n >= MAX_USAGE_ELEMENTS,
            "by_level": dict(sorted(lev.items())), "by_color": col, "by_line_style": sty,
            "by_weight": wt, "by_type": typ, "cells_placed": cells}


# --- library files (dgnlibs / cell libraries) ------------------------------

def expand_file_list(values: list[str], default_exts: tuple[str, ...]) -> list[str]:
    files: list[str] = []
    for v in values:
        for part in split_paths(v):
            p = part
            try:
                if any(c in p for c in "*?"):
                    files.extend(sorted(glob.glob(p)))
                elif os.path.isdir(p):
                    for fn in sorted(os.listdir(p)):
                        if fn.lower().endswith(default_exts):
                            files.append(os.path.join(p, fn))
                elif os.path.isfile(p):
                    files.append(p)
            except Exception:
                continue
    seen, out = set(), []
    for f in files:
        k = os.path.normcase(os.path.abspath(f))
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def dgnlib_variables(variables: dict) -> dict:
    return {k: v for k, v in variables.items()
            if "DGNLIB" in k.upper() and v and not k.upper().endswith(("_DIR", "OUT"))}


def cell_rows(models: list[dict], all_models_are_cells: bool = False) -> list[dict]:
    return [{"name": m.get("name"), "description": m.get("description"),
             "cell_type": m.get("cell_type"), "is_3d": m.get("is_3d")}
            for m in models
            if m.get("is_cell") or (all_models_are_cells and not m.get("is_hidden"))]


def collect_file_standards(dgn_file, full: bool, is_cell_library: bool = False) -> dict:
    """The per-file standards set, used for the seed and every library."""
    d: dict = {}
    d["models"] = section("models", collect_models, dgn_file, False, default=[])
    d["cells"] = cell_rows(d["models"], is_cell_library)
    if not full:
        return d
    d["levels"] = section("levels", collect_levels, dgn_file, None)
    d["text_styles"] = section("text_styles", collect_text_styles, dgn_file)
    d["dimension_styles"] = section("dimension_styles", collect_dimension_styles, dgn_file)
    d["multiline_styles"] = section("multiline_styles", collect_multiline_styles, dgn_file)
    d["display_styles"] = section("display_styles", collect_display_styles, dgn_file)
    d["line_styles"] = section("line_styles", collect_line_styles, dgn_file)
    d["item_types"] = section("item_types", collect_item_types, dgn_file)
    d["element_templates"] = section("element_templates", collect_element_templates, dgn_file)
    d["views"] = section("views", collect_named_views, dgn_file)
    d["dictionary_inventory"] = section("dictionary", collect_dictionary, dgn_file)
    return d


def dump_library(path: str, full: bool, is_cell_library: bool = False) -> dict:
    rec: dict = {"path": path}
    try:
        st = os.stat(path)
        rec["size_bytes"] = st.st_size
        rec["modified"] = _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
    except Exception:
        pass
    crumb(f"  opening {path}")
    try:
        f = open_dgn_readonly(path)
    except Exception as e:  # noqa: BLE001
        record_error(f"open {path}", e)
        rec["_error"] = str(e)
        return rec
    try:
        rec.update(collect_file_standards(f, full, is_cell_library))
    finally:
        del f
    return rec


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def choose_output_dir(dgn_path: str | None) -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.splitext(os.path.basename(dgn_path or "untitled"))[0]
    # (root folder, run subfolder) in order of preference
    candidates = []
    if os.environ.get("STANDARDS_DUMP_DIR"):
        candidates.append((os.environ["STANDARDS_DUMP_DIR"], f"{base}_{stamp}"))
    if OUTPUT_DIR:
        candidates.append((OUTPUT_DIR, f"{base}_{stamp}"))
    if dgn_path:
        candidates.append((os.path.join(os.path.dirname(dgn_path), f"{base}_standards_dump"), stamp))
    p = cfg_get("MS_TMP")
    if p:
        candidates.append((os.path.join(p, f"{base}_standards_dump"), stamp))
    candidates.append((os.path.join(tempfile.gettempdir(), f"{base}_standards_dump"), stamp))
    for i, (root, sub) in enumerate(candidates):
        out = os.path.join(root, sub)
        try:
            os.makedirs(out, exist_ok=True)
            test = os.path.join(out, ".write_test")
            with open(test, "w") as fh:
                fh.write("ok")
            os.remove(test)
            if i > 0:
                NOTES.append(f"Preferred output folder not writable; wrote to {out}")
            return out
        except Exception as e:  # noqa: BLE001
            log(f"WARNING cannot write to {out}: {e}")
            continue
    raise RuntimeError("No writable output directory found")


def write_csv(path: str, rows: list[dict], fields: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})


def write_outputs(result: dict, out_dir: str) -> list[str]:
    written = []
    jp = os.path.join(out_dir, "standards.json")
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False, allow_nan=False,
                  default=lambda o: to_plain(o, 0))
    written.append(jp)

    seed = result.get("seed_file", {})
    # Seed levels plus every DGN library's levels: in a WorkSpace most
    # standard levels live in the dgnlibs, not in the seed itself.
    levels = [{"source": "seed", **l} for l in (seed.get("levels") or {}).get("levels") or []]
    for lib in result.get("dgn_libraries") or []:
        for l in (lib.get("levels") or {}).get("levels") or []:
            levels.append({"source": lib.get("path"), **l})
    if levels:
        p = os.path.join(out_dir, "levels.csv")
        write_csv(p, levels, ["source", "name", "id", "code", "description", "parent_name", "color", "color_rgb",
                              "line_style", "line_style_name", "weight", "display", "frozen", "locked",
                              "plot", "transparency", "display_priority", "element_access",
                              "is_library_level", "library_name"])
        written.append(p)

    variables = (result.get("configuration") or {}).get("variables") or {}
    if variables:
        p = os.path.join(out_dir, "config_variables.csv")
        write_csv(p, [{"name": k, "value": v} for k, v in variables.items()], ["name", "value"])
        written.append(p)

    paths = result.get("paths") or []
    if paths:
        p = os.path.join(out_dir, "paths.csv")
        write_csv(p, paths, ["variable", "path", "exists", "is_dir", "is_file", "wildcard", "matches"])
        written.append(p)

    cell_rows = []
    for c in seed.get("cells") or []:
        cell_rows.append({"source": "seed (cell model)", **c})
    for c in ((seed.get("dictionary_inventory") or {}).get("shared_cell_definitions") or []):
        cell_rows.append({"source": "seed (shared cell)", **c})
    for lib in (result.get("cell_libraries") or []) + (result.get("dgn_libraries") or []):
        for c in lib.get("cells") or []:
            cell_rows.append({"source": lib.get("path"), **c})
    if cell_rows:
        p = os.path.join(out_dir, "cells.csv")
        write_csv(p, cell_rows, ["source", "name", "description", "cell_type", "is_3d"])
        written.append(p)

    sp = os.path.join(out_dir, "summary.txt")
    with open(sp, "w", encoding="utf-8") as fh:
        fh.write(build_summary(result))
    written.append(sp)
    return written


def _count(d, *path):
    for p in path:
        if not isinstance(d, dict):
            return 0
        d = d.get(p)
    if isinstance(d, list):
        return len(d)
    return d if isinstance(d, int) else 0


def build_summary(r: dict) -> str:
    seed = r.get("seed_file", {})
    lines = [
        "MicroStation standards dump",
        "===========================",
        f"Seed file : {r.get('environment', {}).get('active_file')}",
        f"Created   : {r.get('environment', {}).get('timestamp')}",
        f"Product   : {(r.get('environment', {}).get('microstation') or {}).get('_USTN_PRODUCT_NAME')} "
        f"{(r.get('environment', {}).get('microstation') or {}).get('_USTN_PRODUCT_VERSION')}",
        f"Workspace : {(r.get('environment', {}).get('microstation') or {}).get('_USTN_WORKSPACENAME')} / "
        f"WorkSet: {(r.get('environment', {}).get('microstation') or {}).get('_USTN_WORKSETNAME')}",
        "",
        f"Models               : {_count(seed, 'models')}",
        f"Levels (seed)        : {_count(seed, 'levels', 'count')}",
        f"Levels (DGN libs)    : {sum(_count(l, 'levels', 'count') for l in r.get('dgn_libraries') or [])}",
        f"Level filters        : {_count(seed, 'levels', 'level_filters')}",
        f"Text styles          : {_count(seed, 'text_styles', 'count')}",
        f"Dimension styles     : {_count(seed, 'dimension_styles', 'count')}",
        f"Multiline styles     : {_count(seed, 'multiline_styles', 'count')}",
        f"Display styles       : {_count(seed, 'display_styles', 'count')}",
        f"Line styles (file)   : {_count(seed, 'line_styles', 'file')}",
        f"Line styles (system) : {_count(seed, 'line_styles', 'system')}",
        f"Item type libraries  : {_count(seed, 'item_types', 'count')}",
        f"Element templates    : {_count(seed, 'element_templates', 'count')}",
        f"Saved views          : {_count(seed, 'views', 'saved_views')}",
        f"Cell models in seed  : {_count(seed, 'cells')}",
        f"Shared cell defs     : {_count(seed, 'dictionary_inventory', 'shared_cell_definitions')}",
        f"References           : {_count(seed, 'references')}",
        f"Config variables     : {_count(r, 'configuration', 'variable_count')}",
        f"Paths referenced     : {_count(r, 'paths')}",
        f"Cell libraries       : {_count(r, 'cell_libraries')}",
        f"DGN libraries        : {_count(r, 'dgn_libraries')}",
        f"DGNLIB list vars     : {', '.join(sorted((r.get('path_groups') or {}).get('dgnlib_lists') or {})) or 'none found'}",
        "",
    ]
    if r.get("_notes"):
        lines.append("Notes:")
        lines += [f"  - {n}" for n in r["_notes"]]
        lines.append("")
    errs = r.get("_errors") or []
    lines.append(f"Errors/warnings: {len(errs)}")
    for e in errs[:200]:
        lines.append(f"  - {e['where']}: {e['error']}")
    return "\n".join(lines) + "\n"


def notify(msg: str) -> None:
    try:
        MC = api("MessageCenter")
        if MC is not None:
            MC.ShowInfoMessage(msg, msg, False)
    except Exception:
        pass
    log(msg)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> dict | None:
    if _IMPORT_ERRORS and api("ISessionMgr") is None:
        print("This script must be run inside MicroStation (Python Manager).")
        for e in _IMPORT_ERRORS:
            print("  import error:", e)
        return None

    dgn_file = active_dgn_file()
    if dgn_file is None:
        print("No active DGN file - open the seed file first.")
        return None
    # Pick the output folder first so progress.log / crash.log exist from
    # the start: if MicroStation crashes they show where it happened.
    out_dir = choose_output_dir(file_name(dgn_file))
    start_crash_diagnostics(out_dir)
    try:
        return _run(dgn_file, out_dir)
    finally:
        stop_crash_diagnostics(out_dir)


def _run(dgn_file, out_dir: str) -> dict:
    log(f"output folder: {out_dir}")
    model_ref = active_model_ref()
    model = active_model()

    result: dict = {"schema": "microstation-standards-dump/1"}
    result["environment"] = section("environment", collect_environment, dgn_file)

    # Configuration first - many later sections use it
    cfg = section("configuration", collect_configuration, default={"variables": {}})
    result["configuration"] = cfg
    variables = cfg.get("variables") or {}
    result["paths"] = section("paths", analyse_paths, variables, default=[])
    result["path_groups"] = {
        "dgnlib_lists": dgnlib_variables(variables),
        "cells": {k: variables.get(k) for k in ("MS_CELL", "MS_CELLLIST", "MS_CELLOUT", "MS_CELLSELECTORDIR")},
        "seeds": {k: v for k, v in variables.items() if "SEED" in k.upper()},
        "printing": {k: v for k, v in variables.items()
                     if any(t in k.upper() for t in ("PLT", "PRINT", "PEN", "PDF"))},
        "fonts_linestyles": {k: v for k, v in variables.items()
                             if any(t in k.upper() for t in ("FONT", "SYMBRSRC", "LINESTYLE", "SHX"))},
        "materials_rendering": {k: v for k, v in variables.items()
                                if any(t in k.upper() for t in ("MATERIAL", "PATTERN", "RENDER", "BUMP", "LIGHT"))},
        "workspace": {k: v for k, v in variables.items() if k.upper().startswith("_USTN_")},
    }

    # Seed file standards
    seed: dict = {}
    seed["file_info"] = section("file_info", collect_file_info, dgn_file)
    seed["active_model"] = section("active_model", model_info_record, model)
    seed["gcs"] = section("gcs", collect_gcs, model)
    seed["color_table"] = section("color_table", color_table, dgn_file, default=[])
    seed["models"] = section("models", collect_models, dgn_file, True, default=[])
    seed["cells"] = cell_rows(seed["models"])
    seed["levels"] = section("levels", collect_levels, dgn_file, seed["color_table"])
    seed["line_styles"] = section("line_styles", collect_line_styles, dgn_file)
    seed["text_styles"] = section("text_styles", collect_text_styles, dgn_file)
    seed["dimension_styles"] = section("dimension_styles", collect_dimension_styles, dgn_file)
    seed["multiline_styles"] = section("multiline_styles", collect_multiline_styles, dgn_file)
    seed["display_styles"] = section("display_styles", collect_display_styles, dgn_file)
    seed["fonts"] = section("fonts", collect_fonts, dgn_file)
    seed["item_types"] = section("item_types", collect_item_types, dgn_file)
    seed["element_templates"] = section("element_templates", collect_element_templates, dgn_file)
    seed["views"] = section("views", collect_named_views, dgn_file)
    seed["references"] = section("references", collect_references, model_ref, default=[])
    seed["dictionary_inventory"] = section("dictionary", collect_dictionary, dgn_file)
    seed["active_settings"] = section("active_settings", collect_active_settings)
    if DUMP_ACTIVE_MODEL_USAGE:
        lv = (seed.get("levels") or {}).get("levels") or []
        seed["active_model_usage"] = section("active_model_usage", collect_active_usage, model, lv, dgn_file)
    result["seed_file"] = seed

    # Libraries
    opened = 0
    seed_key = os.path.normcase(os.path.abspath(file_name(dgn_file) or ""))
    if DUMP_DGNLIBS:
        libs = expand_file_list(list(dgnlib_variables(variables).values()), (".dgnlib",))
        result["dgn_libraries"] = []
        for p in libs:
            if opened >= MAX_LIBRARY_FILES:
                NOTES.append(f"Library cap {MAX_LIBRARY_FILES} reached; remaining dgnlibs skipped")
                break
            if os.path.normcase(os.path.abspath(p)) == seed_key:
                continue
            log(f"dgnlib {p}")
            result["dgn_libraries"].append(dump_library(p, full=True))
            opened += 1
    if DUMP_CELL_LIBRARIES:
        cell_vars = [variables.get(k) for k in ("MS_CELLLIST", "MS_CELL", "MS_CELLOUT") if variables.get(k)]
        done = {os.path.normcase(os.path.abspath(l["path"])) for l in result.get("dgn_libraries") or []}
        done.add(seed_key)
        cel_files = [f for f in expand_file_list(cell_vars, (".cel",))
                     if f.lower().endswith((".cel", ".dgn", ".dgnlib"))
                     and os.path.normcase(os.path.abspath(f)) not in done]
        result["cell_libraries"] = []
        for p in cel_files:
            if opened >= MAX_LIBRARY_FILES:
                NOTES.append(f"Library cap {MAX_LIBRARY_FILES} reached; remaining cell libraries skipped")
                break
            log(f"cell library {p}")
            result["cell_libraries"].append(
                dump_library(p, full=False, is_cell_library=p.lower().endswith(".cel")))
            opened += 1

    result["_notes"] = list(dict.fromkeys(NOTES))
    result["_errors"] = ERRORS
    result["_elapsed_seconds"] = round(time.time() - _T0, 1)

    files = write_outputs(result, out_dir)
    notify(f"Standards dump complete ({len(ERRORS)} warnings): {out_dir}")
    for f in files:
        log(f"  wrote {f}")
    return result


if __name__ == "__main__":
    main()

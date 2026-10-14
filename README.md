# DumpMicroStationStandards

`dump_standards.py` exports every CAD standard reachable from the active
DGN (seed) file in **MicroStation 2025 (25.00.00.119)** using the built-in
MicroStation Python API (Python 3.12). The output is JSON and CSV that other
tools, such as a Claude Code project, can read.

## Running it

1. Open the seed file in MicroStation, in the WorkSpace and WorkSet whose
   standards you want to capture. The configuration variables, DGN libraries
   and cell libraries come from the session that is currently active.
2. Open **Utilities > Python Manager**, add `dump_standards.py`, and run it.
3. Progress is printed to the Python console. When the dump finishes, the
   Message Center shows the output folder, which by default is
   `...\Documents\Standards_Dump\<seed name>_<timestamp>\`.

Settings are at the top of the script:

| Setting | Default | Purpose |
|---|---|---|
| `OUTPUT_DIR` | `C:\Users\u12770\OneDrive - Metropolitan Water District of So Cal\Documents\Standards_Dump` | Output root. Each run writes to a `<seed name>_<timestamp>` subfolder. If the folder can't be written, the script falls back to `<seed folder>\<seed>_standards_dump\<timestamp>\`, then `MS_TMP`, then the system temp folder. Set it to `None` to write next to the seed. The `STANDARDS_DUMP_DIR` environment variable overrides it. |
| `DUMP_DGNLIBS` | `True` | Open each DGN library in every `*DGNLIBLIST*` variable read-only and dump its standards. |
| `DUMP_CELL_LIBRARIES` | `True` | Open each cell library from `MS_CELLLIST`, `MS_CELL` and `MS_CELLOUT` and list its cells. |
| `DUMP_ACTIVE_MODEL_USAGE` | `True` | Count elements in the active model by level, color, line style, weight, type and cell. |
| `SCAN_CFG_FILES` | `True` | Parse the WorkSpace `.cfg` files to discover configuration variable names. |
| `DEEP_INTROSPECTION` | `False` | Call every zero-argument `Get*`, `Is*` and `Has*` method on each object, with the results in the `all` keys. This calls native methods blindly and can crash MicroStation, so it is off by default. |
| `USE_NATIVE_VARIABLE_ITERATION` | `True` | List configuration variables with `ConfigurationManager.IterateThroughVariables`, which also reports each variable's level and lock (`variable_meta` in the JSON). The `.cfg` scan still runs and fills any gaps. |
| `SKIP_SECTIONS` | empty | Section names to skip, for example `{"active_model_usage"}`. |
| `LIMIT_TO_LEVEL_SCOPE` | `True` | Export only the levels listed in the level scope (see below). Set to `False` to export every level. |
| `LEVEL_SCOPE_CSV` | `None` | Path to a CSV that replaces the built-in level scope. |

## Level scope

Levels are limited to the 314 levels in `levels_scope.csv`:

| Source | Levels |
|---|---|
| seed (the active file) | 2 |
| `MWD_Civil_Survey_Levels.dgnlib` | 293 |
| `MWD_Interface.dgnlib` | 18 |
| `MWD_Border_Levels.dgnlib` | 1 |

The list is built into the script, so only `dump_standards.py` needs to be
copied to MicroStation. To change the scope, put a `levels_scope.csv` next to
the script, or set `LEVEL_SCOPE_CSV`. The CSV needs a `source` column (`seed`
or the DGN library path or file name) and a `name` column. Library files are
matched by file name, and level names are matched without regard to case.

Library levels are read through the seed's own level cache
(`FileLevelCache.GetLibraries()`): these are the DGN libraries MicroStation
has attached to the seed, so the script does not open those files for
levels, and only libraries MicroStation actually uses are included. If no
attached libraries are found, the script falls back to opening the library
files named in the scope and reading their levels. Every other standard
(styles, cells, configuration, paths) is still exported as before.
`summary.txt` and the `level_scope` section of `standards.json` list any
in-scope level that was not found.

## Output files

| File | Contents |
|---|---|
| `standards.json` | Everything. This is the primary output. |
| `levels.csv` | The in-scope levels from the seed file and the DGN libraries, one row per level. The `source` column says which file each level came from. Columns cover by-level symbology (color, line style, weight), the overrides with their on/off flags, display, frozen, hidden, read-only, locate, snap, plot, plot style, transparency, priority, element access, and the file each color and line style is defined in. |
| `config_variables.csv` | Each configuration variable name with its expanded value. |
| `paths.csv` | Each path referenced by a variable, with whether it exists, whether it is a directory, and how many files a wildcard matches. |
| `cells.csv` | Cells from the seed (cell models and shared cells) and from every library. |
| `summary.txt` | Counts, notes and every warning. |

## `standards.json` layout

```
schema                      "microstation-standards-dump/1"
environment                 script/python/product/workspace/workset, active file + model
configuration
  variables                 {NAME: expanded value}
  cfg_files_scanned         [.cfg files read]
  discovery_method
paths                       [{variable, path, exists, is_dir, is_file, wildcard, matches}]
path_groups                 dgnlib_lists, cells, seeds, printing, fonts_linestyles,
                            materials_rendering, workspace (_USTN_*)
seed_file
  file_info                 path, size, modified, DgnFile properties
  active_model              name, type, 2D/3D, master/sub/storage units, UORs,
                            global origin, annotation scale, unit formats
  gcs                       geographic coordinate system (if assigned)
  color_table               [{index, red, green, blue}] x 256
  models                    [{name, description, id, type, cell_type, is_3d,
                              is_hidden, is_cell, model_info, all}]
  cells                     cell models in the seed
  levels                    {count, skipped_out_of_scope, levels:[...]}
                            each level: name, display_name, id, code,
                            description, color(+rgb), line_style(+name),
                            weight, overrides + on/off flags, display, frozen,
                            hidden, read_only, locate, snap, plot,
                            plot_style_id, transparency, priority,
                            element_access, color/line style defined_in
  line_styles               {file:[...], system:[...], resource_files}
  text_styles               {source, count, styles:[{name, id, properties, all}]}
  dimension_styles          {source, count, styles:[...]}
  multiline_styles          {source, count, styles:[{name, profiles, ...}]}
  display_styles            {source, count, styles:[{index, name}]}
  fonts                     {fonts:[...], paths}
  item_types                {libraries:[{name, item_types:[{name, properties}]}]}
  views                     {saved_views:[...], view_groups:[...]}
  references                [{logical_name, file, full_path, model, ...}]
  dictionary_inventory      element counts by type, descriptions,
                            shared_cell_definitions
  active_model_usage        element counts by level/color/style/weight/type/cell
attached_libraries          [{path, read_from, levels}]   levels of the DGN
                            libraries attached to the seed's level cache
dgn_libraries               [{path, size, models, cells, levels (fallback only),
                              text_styles, dimension_styles, multiline_styles,
                              display_styles, item_types, views,
                              dictionary_inventory}]
cell_libraries              [{path, models, cells}]
_notes                      sections the API could not provide
_errors                     [{where, error, traceback}]
```

`null` means that value was not available from the API. `all` keys are
filled only when `DEEP_INTROSPECTION` is on.

Element templates and active settings are not exported: the MicroStation
Python API has no call that lists a file's element templates, and the
active-settings classes the script used to guess at do not exist.

## If MicroStation crashes

The output folder is created before any data is read, and two files are
written there while the script runs:

* `progress.log` lists each section and library file as it starts, so its
  last line shows where the script was when MicroStation died.
* `crash.log` holds the Python traceback written by `faulthandler` at the
  moment of a native crash, including the line of `dump_standards.py` that
  made the failing call. It is deleted after a clean run.

Add the failing section to `SKIP_SECTIONS` to get a dump without it, and
send `crash.log` along when reporting the problem.

## Line styles

Line style tables are read only for the seed: `LineStyleManager.GetLineStyleMap`
for the file and `GetSystemLineStyleMap` for the system/resource styles.
`DgnFile.GetLineStyleMap()` on DGN libraries opened by the script caused
access violations, so it is not called on them. A level's `line_style_name`
is looked up by number in the seed's and system tables
(`GetLineStyleEntry`); a style that only exists inside a DGN library keeps
its number with an empty name. Library levels take `color_rgb` from the
seed's color table; `color_defined_in` and `line_style_defined_in` name the
file each setting comes from.

## Robustness

API calls follow the signatures in Bentley's MicroStation 2025 Python stub
files (`MSPyDgnPlatform.pyi`, `MSPyMstnPlatform.pyi` and the other modules
in `C:/ProgramData/Bentley/PowerPlatformPython/Examples/MicroStation/Intellisense`).
In addition:

* Classes are looked up module by module, `MSPyDgnPlatform` first, because
  some names exist in two modules (`MSPyMstnPlatform.DgnFile` is a helper
  class that cannot open files).
* Each section runs in its own `try`/`except`. If a section fails, the error
  is recorded in `_errors` and the dump continues.
* Library files are opened **read-only**. The script never modifies the seed
  file or any library.

If a section comes back empty, check `_errors` and `_notes` in the JSON or
in `summary.txt`. They show which API call was not available in your build.

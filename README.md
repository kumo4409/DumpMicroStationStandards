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
   Message Center shows the output folder.

Settings are at the top of the script:

| Setting | Default | Purpose |
|---|---|---|
| `OUTPUT_DIR` | `None` | Output root. When `None`, the script uses `<seed folder>/<seed>_standards_dump/<timestamp>/`, falling back to `MS_TMP` and then the system temp folder. The `STANDARDS_DUMP_DIR` environment variable also overrides it. |
| `DUMP_DGNLIBS` | `True` | Open each DGN library in every `*DGNLIBLIST*` variable read-only and dump its standards. |
| `DUMP_CELL_LIBRARIES` | `True` | Open each cell library from `MS_CELLLIST`, `MS_CELL` and `MS_CELLOUT` and list its cells. |
| `DUMP_ACTIVE_MODEL_USAGE` | `True` | Count elements in the active model by level, color, line style, weight, type and cell. |
| `SCAN_CFG_FILES` | `True` | Parse the WorkSpace `.cfg` files to discover configuration variable names. |
| `DEEP_INTROSPECTION` | `True` | Call every zero-argument `Get*`, `Is*` and `Has*` method on each object. The results go in the `all` keys. |

## Output files

| File | Contents |
|---|---|
| `standards.json` | Everything. This is the primary output. |
| `levels.csv` | Seed-file levels, one row per level, with symbology and flags. |
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
  levels                    {count, levels:[...], level_filters:[...]}
                            each level: name, id, code, description, parent,
                            color(+rgb), line_style(+name), weight, overrides,
                            display/frozen/locked/plot, transparency, priority,
                            element_access, library info, all
  line_styles               {file:[...], system:[...], resource_files}
  text_styles               {source, count, styles:[{name, id, properties, all}]}
  dimension_styles          {source, count, styles:[...]}
  multiline_styles          {source, count, styles:[{name, profiles, ...}]}
  display_styles            {source, count, styles:[...]}
  fonts                     {fonts:[...], paths}
  item_types                {libraries:[{name, item_types:[{name, properties}]}]}
  element_templates         {templates:[{path, name, is_group, all}]}
  views                     {saved_views:[...], view_groups:[...]}
  references                [{logical_name, file, full_path, model, ...}]
  dictionary_inventory      element counts by type, descriptions,
                            shared_cell_definitions
  active_settings           active parameters, if the API exposes them
  active_model_usage        element counts by level/color/style/weight/type/cell
dgn_libraries               [{path, size, models, cells, levels, text_styles,
                              dimension_styles, multiline_styles, display_styles,
                              line_styles, item_types, element_templates, views,
                              dictionary_inventory}]
cell_libraries              [{path, models, cells}]
_notes                      sections the API could not provide
_errors                     [{where, error, traceback}]
```

`null` means that value was not available from the API. Each `all` key holds
the raw result of introspecting the object, so properties the explicit fields
miss are still captured.

## Robustness

The MicroStation Python bindings wrap the C++ DgnPlatform API, and method
names and signatures vary between builds. For that reason:

* Each API call tries the known spellings and argument shapes in turn, for
  example out-parameter `WString` vs return value, and `(status, value)`
  tuples.
* Each section runs in its own `try`/`except`. If a section fails, the error
  is recorded in `_errors` and the dump continues.
* Library files are opened **read-only**. The script never modifies the seed
  file or any library.

If a section comes back empty, check `_errors` and `_notes` in the JSON or
in `summary.txt`. They show which API call was not available in your build.

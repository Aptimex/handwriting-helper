'''
Bambu Lab printers won't run a bare .gcode file sent to them: they take a .gcode.3mf, the zip
Bambu Studio writes when it slices, with the G-code inside at Metadata/plate_1.gcode.

So rather than build one from scratch, a .gcode.3mf that Bambu Studio sliced for the printer
is used as a template: it is copied entry by entry, with that one file swapped for our G-code.
Everything else in it (thumbnails, the slicer's settings, the printer model) is passed through
untouched; only the G-code is run.
'''

import hashlib
import zipfile
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "template.gcode.3mf"
GCODE_ENTRY = "Metadata/plate_1.gcode"
MD5_ENTRY = GCODE_ENTRY + ".md5"


def missing_template(path):
    '''What to tell someone who has no template at `path` yet.'''
    return ("no Bambu template at {}. Bambu printers only accept a .gcode.3mf sliced for "
            "their own model, so make one first:\n"
            "  1. In Bambu Studio, select your printer and slice any small object.\n"
            "  2. Export the sliced plate as a .gcode.3mf (File > Export > Export plate sliced "
            "file).\n"
            "  3. Save it as {}".format(path, path))


class TemplateError(ValueError):
    pass


def wrap_gcode(gcode, target, template=None):
    '''
    Write the G-code file `gcode` into a copy of the .gcode.3mf `template` (default: TEMPLATE)
    saved as `target`.
    '''
    template = Path(template) if template else TEMPLATE
    if not template.is_file():
        raise TemplateError(missing_template(template))
    data = Path(gcode).read_bytes()
    try:
        with zipfile.ZipFile(template) as src:
            names = src.namelist()
            if GCODE_ENTRY not in names:
                raise TemplateError("{} has no {}; is it a sliced .gcode.3mf?".format(
                    template, GCODE_ENTRY))
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
                for info in src.infolist():
                    if info.filename == GCODE_ENTRY:
                        body = data
                    elif info.filename == MD5_ENTRY:
                        # The printer doesn't seem to check it, but keep it true anyway.
                        body = hashlib.md5(data).hexdigest().upper().encode("ascii")
                    else:
                        body = src.read(info)
                    out.writestr(info, body, compress_type=zipfile.ZIP_DEFLATED)
    except zipfile.BadZipFile as exc:
        raise TemplateError("{} is not a .gcode.3mf: {}".format(template, exc))

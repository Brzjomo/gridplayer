"""The colour each recording's sound is drawn in, and where it is kept.

A colour says nothing about what a file holds -- it is the viewer telling
one recording from another at a glance -- so it is not part of a playlist:
two playlists naming the same recordings draw them the same way, whether or
not either of them is the one they were coloured in. What is kept is a path
and a colour, in the settings, and only for the recordings somebody has
actually coloured: nothing here writes a colour nobody chose.

Nothing about this needs a player or a window, so what it does can be
checked without either.
"""

import os

from pydantic import BaseModel, Field

# The colours a recording is drawn in where it has not been given one of its
# own: the bright set the bookmarks are given, which are chosen to be told
# apart from each other on either a light theme or a dark one. Six of them,
# and a grid of more than six uncoloured recordings draws them round again --
# which is a reason to colour one by hand rather than a thing to be fixed by
# adding colours nobody can name.
SPECTRUM_COLORS = (
    "#ff3d3d",
    "#3d8bff",
    "#3ddc5a",
    "#b36bff",
    "#1fd6e8",
    "#ff4fb8",
)

# How many recordings' colours are kept. A colour is nine characters of a
# settings file, so this is under two kilobytes: far more than a grid ever
# shows at once and far less than a settings file nobody can read.
KEPT_COLORS = 200


class SpectrumColors(BaseModel):
    """A colour per recording, by the path of the file it was read from."""

    colors: dict[str, str] = Field(default_factory=dict)


def _key(uri) -> str:
    """What tells one recording from another.

    The path as the platform spells it, since a file coloured on Windows is
    the same file whether the path was typed in one case or the other.
    """

    return os.path.normcase(str(uri))


def spectrum_colors() -> dict[str, str]:
    """Every colour that has been given, by the path it was given to."""

    # imported here: the settings read this model to make their defaults,
    # and a module of its own that read them back at import would be a
    # circle
    from gridplayer.settings import Settings

    return Settings().get("playlist/spectrum_colors").colors


def spectrum_color(uri) -> str | None:
    """The colour a recording is drawn in, where one was given to it."""

    return spectrum_colors().get(_key(uri))


def remember_spectrum_color(uri, color: str | None):
    """Give a recording a colour, or take away the one it has.

    Whatever is written is moved to the end of what is kept, so that the
    colours in use are the ones that survive being kept to the last
    `KEPT_COLORS` of them.
    """

    from gridplayer.settings import Settings

    colors = dict(spectrum_colors())
    key = _key(uri)

    colors.pop(key, None)

    if color is not None:
        colors[key] = color

        while len(colors) > KEPT_COLORS:
            colors.pop(next(iter(colors)))

    Settings().set("playlist/spectrum_colors", SpectrumColors(colors=colors))

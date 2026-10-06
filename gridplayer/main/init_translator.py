import logging

from PyQt5.QtCore import QLibraryInfo, QLocale, QTranslator

from gridplayer.params import env
from gridplayer.settings import Settings


def init_translator(app):
    log = logging.getLogger(__name__)

    lang = Settings().get("player/language")
    if lang == "en_US":
        return

    log.debug(f"Loading translation for {lang}")

    if env.IS_PYINSTALLER and env.IS_WINDOWS:
        qt_translations_path = str(
            env.PYINSTALLER_LIB_ROOT / "PyQt5" / "Qt5" / "translations"
        )
    else:
        qt_translations_path = QLibraryInfo.location(QLibraryInfo.TranslationsPath)
    log.debug(f"QT translations path: {qt_translations_path}")

    # Qt's own strings are asked for in two places: what this application
    # ships, then the Qt the machine has. The application's own copy is what
    # makes Simplified Chinese possible at all -- neither a PyQt5 wheel nor a
    # Qt release carries a qtbase_zh_CN.qm, while the translation itself is
    # all but complete upstream. See dev_docs/translations.md.
    qt_paths = (
        str(env.RESOURCES_DIR / "translations"),
        qt_translations_path,
    )

    translator_qt = QTranslator(app)
    if any(translator_qt.load(QLocale(lang), "qtbase_", "", path) for path in qt_paths):
        app.installTranslator(translator_qt)
    else:
        log.warning(f"Failed to load QT translation for {lang}")

    translator = QTranslator(app)
    if translator.load(lang, str(env.RESOURCES_DIR / "translations")):
        app.installTranslator(translator)
    else:
        log.warning(f"Failed to load translation for {lang}")

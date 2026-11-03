# bot.py — загрузчик. Код разбит на файлы в папке modules/. Они выполняются по порядку
# в одном общем пространстве имён (как будто это один файл), поэтому порядок важен.
import os as _os

_MODULES = [
    "01_core.py",
    "02_payments.py",
    "03_roles_keys.py",
    "04_media_cache.py",
    "05_commands.py",
    "06_admin_faq.py",
    "07_feedback.py",
    "08_achievements.py",
    "09_retention.py",
    "10_translate.py",
    "11_voice_files.py",
    "12_lists.py",
    "13_links.py",
    "14_messages.py",
    "15_callbacks.py",
    "16_business.py",
    "17_run.py",
]
_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "modules")
for _name in _MODULES:
    _p = _os.path.join(_DIR, _name)
    with open(_p, encoding="utf-8") as _f:
        exec(compile(_f.read(), _p, "exec"), globals())

if __name__ == "__main__":
    asyncio.run(main())

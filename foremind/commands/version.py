from foremind import __version__


def register(sub):
    sub.add_parser("version", help="print the foremind version").set_defaults(func=lambda args: print(__version__))

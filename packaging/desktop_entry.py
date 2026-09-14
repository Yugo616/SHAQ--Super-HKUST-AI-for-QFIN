import sys


if __name__ == "__main__":
    if getattr(sys, 'frozen', False):
        import velopack
        # Fast-exit install/update hooks must run before importing the desktop.
        # Downloaded updates never auto-apply outside the admission gate.
        velopack.App().set_auto_apply_on_startup(False).run()
    from shaq_daily_oracle.desktop import main
    raise SystemExit(main())

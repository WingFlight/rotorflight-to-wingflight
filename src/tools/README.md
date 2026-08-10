Place `dfu-util.exe` in this folder before building if you want the standalone
EXE to bundle its own flasher.

If this folder does not contain `dfu-util.exe`, the app will look for
`dfu-util.exe` or `dfu-util` on `PATH`, then auto-download the official Windows
binary when running on Windows.

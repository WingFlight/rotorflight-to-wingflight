# GD32/AT32 DFU Driver (best-effort)

This directory contains a generic Microsoft WinUSB co-installer INF for the
GD32/AT32 DFU bootloader USB ID (`VID_28E9&PID_0189`) used by some flight
controller targets, alongside the STM32 DFU driver in `../stm32/`.

## Important: unsigned driver

Unlike `../stm32/STM32Bootloader.inf`, which ships an official
STMicroelectronics-signed catalog file, **there is no vendor-supplied,
Microsoft-signed catalog for this GD32/AT32 driver**. `GD32Bootloader.inf` was
authored from scratch using the same generic libusb/WinUSB template structure
as the STM32 package (swapping only the USB ID and device interface GUID) —
it is not derived from any GigaDevice/Artery driver package.

Windows' driver-signature-enforcement policy may block or warn on installing
an unsigned driver, depending on the machine (Secure Boot state, Windows
edition, group policy). Rotorflight to Wingflight treats a failed GD32/AT32
install as a soft failure and points the user at
[Zadig](https://zadig.akeo.ie/) as a manual fallback — it never claims success
it hasn't verified.

## License

This INF is an original, from-scratch file using only the standard Microsoft
WinUSB co-installer INF structure (`Include=winusb.inf`, `Needs=WINUSB.NT`).
It contains no GigaDevice/Artery proprietary content.

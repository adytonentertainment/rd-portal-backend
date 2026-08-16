# Chromaprint (fpcalc) Binaries

This directory contains the `fpcalc` binary from Chromaprint v1.5.1, which is used for audio fingerprinting with AcoustID.

## Structure

```
bin/chromaprint/
├── windows/
│   └── fpcalc.exe      # Windows 64-bit binary
├── linux/
│   └── fpcalc          # Linux 64-bit binary
└── README.md
```

## Usage

The application automatically detects the platform and uses the appropriate binary:
- **Windows**: `bin/chromaprint/windows/fpcalc.exe`
- **Linux**: `bin/chromaprint/linux/fpcalc`

## Version Information

- **Chromaprint Version**: 1.5.1
- **Release Date**: December 23, 2021
- **Built with**: FFmpeg 4.4.1
- **Source**: https://github.com/acoustid/chromaprint/releases/tag/v1.5.1

## License

Chromaprint is licensed under the MIT License.

## Why These Binaries Are Included

These binaries are included in the repository to:
1. Ensure consistent behavior across development and production environments
2. Avoid requiring system-level installation of chromaprint
3. Support easy deployment without external dependencies

## Updating

To update to a newer version of Chromaprint:

1. Download the latest release from: https://github.com/acoustid/chromaprint/releases
2. Extract the Windows and Linux binaries
3. Replace the files in `windows/` and `linux/` directories
4. Update this README with the new version information

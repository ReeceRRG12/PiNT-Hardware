# Contributing to PiNT Hardware

Thanks for your interest in contributing! PiNT is an early-stage open source project and all contributions are welcome.

## Reporting bugs

Please open a [GitHub issue](https://github.com/ReeceRRG12/PiNT-Hardware/issues) and include:

- Pi model and OS version (`uname -a`)
- Switch vendor and model (if relevant)
- What you expected to happen vs what actually happened
- Any error output from `sudo journalctl -u pint -n 50`

## Suggesting features

Open an issue with the `enhancement` label. Describe the use case — what problem does it solve on the bench?

## Submitting a pull request

1. Fork the repo and create a branch from `main`
2. Make your changes — keep them focused on a single thing
3. Test on a real Pi if possible (Scapy behaviour can differ from a dev machine)
4. Open a PR with a clear description of what changed and why

## Code style

- Python: follow the existing style (no external linters configured)
- Keep things simple — this runs on a Pi Zero-class device, not a server
- No new dependencies without a good reason

## Contact

For anything else, reach out at [reece@pinetworktools.com](mailto:reece@pinetworktools.com).

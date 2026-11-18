# Third-party notices

## phone-mcp

Copyright 2023 hao. Licensed under the Apache License, Version 2.0. A copy of
the license is included in [LICENSE](LICENSE).

The app-control extra installs [phone-mcp 0.3.6](https://pypi.org/project/phone-mcp/0.3.6/)
as an external Python dependency. Upstream source and license:
[hao-cyber/phone-mcp](https://github.com/hao-cyber/phone-mcp/tree/v0.3.6),
[upstream LICENSE](https://github.com/hao-cyber/phone-mcp/blob/v0.3.6/LICENSE).
The pinned release tag resolves to `8923527759dd87158ba445a4c783169acf40b5bf`.

Phone Agent's MCP adapter subclasses phone-mcp's `UIElement` and supplies its
own Android screen capture, rotation-aware coordinate handling, device
selection, and MCP tool registration. These changes live in Phone Agent's
adapter. The installed upstream files are unmodified. No upstream NOTICE file
was present at the reviewed release tag.

Other optional packages and downloaded model weights retain their own
licenses. This repository distributes neither model weights nor Piper.

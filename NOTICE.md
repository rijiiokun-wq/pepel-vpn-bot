# Attribution and third-party notices

## Project origin and modifications

PEPEL VPN is a modified version of **VLESS Shop Bot**, published by
[evansvl and the upstream contributors](https://github.com/evansvl/vless-shopbot).
The upstream source examined for this release preparation is commit
[`0b2840df096831032c54f7b8218987c46d874f9b`](https://github.com/evansvl/vless-shopbot/tree/0b2840df096831032c54f7b8218987c46d874f9b).

This version includes modifications by PEPEL VPN contributors during 2026.
Release preparation date: **2026-10-27**. Changes include separate bot runtimes,
payment processing and recovery, subscription issuance safeguards, additional
provider integrations, mobile-device authorization, and administrative tools.
This modified version is maintained separately from the upstream project.

The project is distributed under **GNU GPL version 3**; see [LICENSE](LICENSE).
The license file is reproduced verbatim from the upstream commit cited above
(Git blob `f288702d2fa16d3cdf0035b15a9fcbc552cd88e7`). Original authors and
contributors retain their respective rights. This notice does not assign their
rights to PEPEL VPN or claim exclusive authorship of the original project.

The menu icon `src/shop_bot/webhook_server/static/img/burger.png` is retained
unchanged from that same GPLv3 upstream project. Its Git blob matches the pinned
upstream asset: `9dfa16c22baf8f0e8ce94f989c0fed465e089d77` (24,000 bytes).

## Bundled JavaScript

**Chart.js 4.4.7**, Copyright (c) 2014-2024 Chart.js Contributors, is supplied
under the MIT License. Preserve its source header and the accompanying license:

- `src/shop_bot/webhook_server/static/vendor/chart.umd.min.js`
- [CHART-LICENSE.md](src/shop_bot/webhook_server/static/vendor/CHART-LICENSE.md)

The license for this component is retained alongside it; the project license
does not replace the component's own copyright and permission notices.

The Chart.js bundle also contains **@kurkle/color 0.3.2**. Its preserved bundle
header credits Copyright (c) 2023 Jukka Kurkela and the MIT License. The
[upstream license at v0.3.2](https://github.com/kurkle/color/blob/v0.3.2/LICENSE.md)
is reproduced below with its original copyright years:

> The MIT License (MIT)
>
> Copyright (c) 2018-2021 Jukka Kurkela
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Direct Python runtime dependencies

The following preliminary inventory was checked on 2026-10-27 against the
project's pinned direct requirements and installed distribution metadata. These
packages are installed as dependencies; this repository does not claim authorship
of them. Consult the license text in each distribution for its terms.

| Distribution | Version | License reported by the distribution |
| --- | --- | --- |
| aiogram | 3.28.2 | MIT |
| Flask | 3.1.3 | BSD-3-Clause |
| py3xui | 0.4.0 | MIT |
| pyotp | 2.9.0 | MIT |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| qrcode | 8.2 | BSD; the bundled LICENSE contains the three-clause conditions |
| yookassa | 3.5.0 | MIT |
| aiosend | 3.0.6 | MIT |
| aiohttp | 3.13.4 | Apache-2.0 AND MIT |
| aiohttp-sse-client | 0.2.1 | Apache-2.0 |
| pytonconnect | 0.3.2 | Apache-2.0 |
| plategaio | 1.0.4 | MIT |

This table covers direct Python runtime dependencies, not all transitive
dependencies, optional tooling, operating-system packages, or container layers.
For example, the `qrcode[pil]` extra installs additional packages. Their own
notices must also be retained when distributing them or a built image.

The application also imports **requests 2.34.2** directly from the environment,
where it is currently installed through transitive requirements. Both `uv.lock`
and the checked installed distribution specify 2.34.2; its distribution metadata
reports **Apache-2.0**. It is included here as an additional runtime dependency
notice, not as a claim that it is separately declared in `pyproject.toml`.

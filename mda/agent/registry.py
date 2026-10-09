"""可信 NI 43-101 PDF 证据注册表。

只收录已实测验证（真实 HTTP 200 + %PDF 魔数 + 封面核对）的 PDF，
每条记录带 verified_at。LLM 不得自造 PDF URL，只能从本注册表选择。
验证过程见 DATA_SOURCES.md。
"""

from __future__ import annotations

import json
from pathlib import Path

from mda.common.logging import get_logger
from mda.common.settings import PROJECT_ROOT

log = get_logger(__name__)

REGISTRY_PATH = PROJECT_ROOT / "data" / "known_pdfs.json"

_DEFAULT_REGISTRY = {
    "documents": [
        {
            "id": "sigma-grota-do-cirilo-2023",
            "url": (
                "https://sigmalithiumresources.com/wp-content/uploads/2023/05/"
                "2023-01-SGML-Updated-Technical-Report-1.pdf"
            ),
            "standard": "NI 43-101",
            "commodity": "lithium",
            "project": "Grota do Cirilo",
            "property": "Sigma Lithium, Minas Gerais, Brazil",
            "effective_date": "2022-10-31",
            "issue_date": "2023-01-16",
            "verified_at": "2026-10-08",
            "size_mb": 17.0,
            "note": (
                "能力验收用锂矿报告（非 Pilbara 目标矿区）。Pilbara 矿企"
                "（如 Pilbara Minerals）采用 ASX/JORC 标准，无 NI 43-101 报告。"
            ),
            "enabled": True,
        },
        {
            "id": "probe-gold-novador-2024",
            "url": "https://novador.ca/wp-content/uploads/probe-43-101-pea-march-26.pdf",
            "standard": "NI 43-101",
            "commodity": "gold",
            "project": "Novador",
            "property": "Probe Gold, Quebec, Canada",
            "effective_date": "2024-02-13",
            "issue_date": "2024-03-26",
            "verified_at": "2026-10-08",
            "size_mb": 15.7,
            "note": "金矿 PEA 报告（能力验收备用）。",
            "enabled": True,
        },
        {
            "id": "lithium-americas-thacker-pass-2018",
            "url": (
                "https://investors.lithium-argentina.com/static-files/"
                "fe7e604b-3b87-4690-9772-3b264e888d44"
            ),
            "standard": "NI 43-101",
            "commodity": "lithium",
            "project": "Thacker Pass",
            "property": "Lithium Americas, Nevada, USA",
            "effective_date": "2018-08-01",
            "issue_date": "",
            "verified_at": "2026-10-08",
            "size_mb": 5.1,
            "note": "SEC 6-K 包裹的 NI 43-101 PFS（277 页），报告封面在第 4 页。",
            "enabled": False,
        },
        {
            "id": "patriot-shaakichiuwaanaan-fs-2025",
            "url": (
                "https://www.pmet.ca/wp-content/uploads/2025/11/FS_Technical_Report_43-101-1.pdf"
            ),
            "standard": "NI 43-101",
            "commodity": "lithium",
            "project": "Shaakichiuwaanaan",
            "property": "Patriot Battery Metals, Quebec, Canada",
            "effective_date": "",
            "issue_date": "2025-11",
            "verified_at": "2026-10-08",
            "size_mb": 34.0,
            "note": "34MB，超默认 25MB 体积偏好，默认关闭。",
            "enabled": False,
        },
    ]
}


def load_registry(path: Path | None = None) -> list[dict]:
    path = path or REGISTRY_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        docs = payload.get("documents", [])
        if not isinstance(docs, list):
            raise ValueError("registry documents 必须是数组")
        return [d for d in docs if isinstance(d, dict)]
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log.warning("registry_fallback", reason=str(exc))
        return list(_DEFAULT_REGISTRY["documents"])

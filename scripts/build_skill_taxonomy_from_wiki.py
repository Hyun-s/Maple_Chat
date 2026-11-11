#!/usr/bin/env python3
"""Build verified job skill taxonomy and catalog from Namu Wiki."""
from __future__ import annotations

import glob
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "src" / "maple_chat" / "knowledge" / "data"
GUIDES_DIR = PROJECT_ROOT / "src" / "maple_chat" / "skills" / "guides"
TAXONOMY_PATH = DATA_DIR / "job-taxonomy-v1.json"
OUTPUT_CATALOG_PATH = DATA_DIR / "job-skill-catalog-v1.json"

# 1. Pure Universal Common Skills (Available to all jobs)
UNIVERSAL_COMMON_SKILLS = [
    # Seed Rings
    "리스트레인트 링", "웨폰퍼프 링", "컨티뉴어스 링", "크라이시스-HM링",
    "리스크테이커 링", "듀라빌리티 링", "크리데미지 링",
    # Decent 5th skills
    "쓸만한 샤프 아이즈", "쓸만한 컴뱃 오더스", "쓸만한 윈드 부스터",
    "쓸만한 어드밴스드 블레스", "쓸만한 미스틱 도어", "쓸만한 하이퍼 바디",
    # Erda 5th skills
    "에르다 노바", "에르다 파운틴", "에르다 샤워", "블링크", "로프 커넥트", "에르다의 의지",
    # Common link & buffs
    "소울 컨트랙트", "메이플 용사", "용사의 의지", "에픽 어드벤처", "글로리 오브 가디언즈",
    "메이플 여신의 축복", "여제 시그너스의 축복", "이계 여신의 축복", "이계의 여신 축복",
    "그란디스 여신의 축복", "프리드의 가호", "여제의 축복"
]

# 2. Strict Job Family Common Skills (Thief, Warrior, Mage, Archer, Pirate)
FAMILY_COMMON_SKILLS = {
    "도적": ["레디 투 다이", "얼티밋 다크사이드", "쉐도우 워커", "베놈 버스트"],
    "마법사": ["오버로드 마나", "에테리얼 폼", "언스태이블 메모라이즈"],
    "전사": ["오라 웨폰", "바디 오브 스틸", "임펜트러블 빌리프"],
    "궁수": ["가이디드 애로우", "크리티컬 리인포스", "이볼브"],
    "해적": ["로디드 다이스", "오버 드라이브", "파이렛 플래그"]
}

def get_job_entities() -> list[dict[str, Any]]:
    # Assign families
    job_family_lookup = {
        "히어로": "전사", "팔라딘": "전사", "다크나이트": "전사", "소울마스터": "전사",
        "미하일": "전사", "블래스터": "전사", "데몬 슬레이어": "전사", "데몬 어벤져": "전사",
        "아란": "전사", "카이저": "전사", "아델": "전사", "제로": "전사",
        "아크메이지(불,독)": "마법사", "아크메이지(썬,콜)": "마법사", "비숍": "마법사",
        "플레임위자드": "마법사", "배틀메이지": "마법사", "에반": "마법사", "루미너스": "마법사",
        "일리움": "마법사", "라라": "마법사", "키네시스": "마법사",
        "보우마스터": "궁수", "신궁": "궁수", "패스파인더": "궁수", "윈드브레이커": "궁수",
        "와일드헌터": "궁수", "메르세데스": "궁수", "카인": "궁수",
        "나이트로드": "도적", "섀도어": "도적", "듀얼블레이드": "도적", "나이트워커": "도적",
        "팬텀": "도적", "카데나": "도적", "칼리": "도적", "호영": "도적",
        "바이퍼": "해적", "캡틴": "해적", "캐논슈터": "해적", "스트라이커": "해적",
        "메카닉": "해적", "은월": "해적", "엔젤릭버스터": "해적", "아크": "해적",
        "제논": "도적",  # Hybrid 도적/해적
        "렌": "전사", "레테": "마법사"
    }

    return [{"name": name, "family": fam} for name, fam in job_family_lookup.items()]

def fetch_wiki_html(job_name: str) -> tuple[str, str] | tuple[None, None]:
    candidates = [
        f"{job_name}/스킬",
        f"{job_name}(메이플스토리)/스킬",
        f"{job_name.replace(' ', '')}/스킬",
        f"{job_name.replace(' ', '')}(메이플스토리)/스킬",
    ]
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    for c in candidates:
        url = 'https://namu.wiki/w/' + urllib.parse.quote(c)
        try:
            req = urllib.request.Request(url, headers=headers)
            res = urllib.request.urlopen(req, timeout=4)
            if res.status == 200:
                return c, res.read().decode('utf-8', errors='ignore')
        except Exception:
            continue
    return None, None

def parse_wiki_headings(html: str) -> dict[str, Any]:
    headings = re.findall(r'<h[1-6][^>]*>(.*?)</h[1-6]>', html)
    parsed = {
        '1st_skills': [],
        '2nd_skills': [],
        '3rd_skills': [],
        '4th_skills': [],
        'hyper_actives': [],
        '5th_skills': [],
        '6th_origin': None,
        '6th_mastery': [],
        '6th_skill_cores': [],
        'all_skills': set()
    }
    current_section = None
    current_subsection = None

    for h in headings:
        clean = re.sub(r'<[^>]+>', '', h).strip()
        clean = clean.replace('&#91;', '[').replace('&#93;', ']')
        clean = re.sub(r'\s*\[편집\].*$', '', clean).strip()

        top_m = re.match(r'^[0-9]+\.\s*(0차|1차|2차|3차|4차|하이퍼 스킬|5차|6차)', clean)
        if top_m:
            current_section = top_m.group(1)
            current_subsection = None
            continue

        sub_m = re.match(r'^[0-9]+\.[0-9]+\.\s*(직업 코어|공용 코어|강화 코어|마스터리 코어|스킬 코어|공격 / 버프 액티브)', clean)
        if sub_m:
            current_subsection = sub_m.group(1)
            continue

        item_m = re.match(r'^[0-9]+(?:\.[0-9]+)+\.\s*(.+)$', clean)
        if item_m:
            raw_skill = item_m.group(1).strip()
            skill_name = re.sub(r'\([^)]*\)', '', raw_skill).strip()
            if not skill_name or '개요' in skill_name or '삭제된' in skill_name:
                continue

            if current_section == '4차':
                parsed['4th_skills'].append(skill_name)
                parsed['all_skills'].add(skill_name)
            elif current_section == '하이퍼 스킬' and current_subsection == '공격 / 버프 액티브':
                parsed['hyper_actives'].append(skill_name)
                parsed['all_skills'].add(skill_name)
            elif current_section == '5차' and current_subsection == '직업 코어':
                parsed['5th_skills'].append(skill_name)
                parsed['all_skills'].add(skill_name)
            elif current_section == '6차':
                if current_subsection == '마스터리 코어':
                    parsed['6th_mastery'].append(skill_name)
                    parsed['all_skills'].add(skill_name)
                elif current_subsection == '스킬 코어':
                    if not parsed['6th_origin']:
                        parsed['6th_origin'] = skill_name
                    else:
                        parsed['6th_skill_cores'].append(skill_name)
                    parsed['all_skills'].add(skill_name)
            elif current_section in ['1차', '2차', '3차']:
                parsed['all_skills'].add(skill_name)

    return parsed

def main():
    jobs = get_job_entities()
    print(f"[INFO] Building wiki-grounded skill taxonomy for {len(jobs)} jobs...")

    # Load existing catalog to preserve verified v_matrix ground truth
    existing_v_matrix = {}
    if OUTPUT_CATALOG_PATH.exists():
        with open(OUTPUT_CATALOG_PATH, "r", encoding="utf-8") as f:
            old_cat = json.load(f)
        for jn, jd in old_cat.get("jobs", {}).items():
            if "v_matrix" in jd:
                existing_v_matrix[jn] = jd["v_matrix"]

    # Also maintain backward-compatible common_skills key
    final_catalog = {
        "version": "2.0-wiki-grounded",
        "source": "Namu Wiki Skill Tree + Nexon Official Job Taxonomy",
        "total_jobs": len(jobs),
        "common_skills": sorted(list(set(UNIVERSAL_COMMON_SKILLS).union(*FAMILY_COMMON_SKILLS.values()))),
        "universal_common_skills": UNIVERSAL_COMMON_SKILLS,
        "family_common_skills": FAMILY_COMMON_SKILLS,
        "jobs": {}
    }

    success_count = 0
    for idx, item in enumerate(jobs):
        jn = item["name"]
        fam = item["family"]
        c_pat, html = fetch_wiki_html(jn)
        if not html:
            print(f"  [{idx+1}/{len(jobs)}] {jn}: Wiki fetch FAILED")
            continue

        parsed = parse_wiki_headings(html)
        job_skills = parsed['all_skills']

        # Get family common and universal common
        fam_skills = FAMILY_COMMON_SKILLS.get(fam, [])
        # For hybrid 제논: include pirate common as well
        if jn == "제논":
            fam_skills = list(set(fam_skills + FAMILY_COMMON_SKILLS.get("해적", [])))

        whitelist = sorted(list(set(list(job_skills) + fam_skills + UNIVERSAL_COMMON_SKILLS)))

        v_matrix = existing_v_matrix.get(jn, {})
        # If v_matrix has specific 5th/6th skills, ensure they are also in whitelist
        for k in ["스킬_코어", "6차_코어"]:
            for sk in v_matrix.get(k, []):
                cleaned = re.sub(r"^(오리진|마스터리 코어|스킬 코어)[\s:]*", "", sk).strip()
                cleaned = re.sub(r"\([^)]*\)", "", cleaned).strip()
                if cleaned:
                    whitelist.append(cleaned)
        for k in ["강화_코어"]:
            for ec in v_matrix.get(k, []):
                for p in re.split(r"[/,+]", ec):
                    cp = p.strip()
                    if cp and len(cp) >= 2:
                        whitelist.append(cp)
        whitelist = sorted(list(set(whitelist)))

        final_catalog["jobs"][jn] = {
            "job_family": fam,
            "wiki_page": c_pat,
            "v_matrix": v_matrix,
            "taxonomy": {
                "1st_skills": parsed['1st_skills'],
                "2nd_skills": parsed['2nd_skills'],
                "3rd_skills": parsed['3rd_skills'],
                "4th_skills": parsed['4th_skills'],
                "hyper_actives": parsed['hyper_actives'],
                "5th_job_cores": parsed['5th_skills'],
                "6th_origin": parsed['6th_origin'],
                "6th_mastery": parsed['6th_mastery'],
                "6th_skill_cores": parsed['6th_skill_cores'],
            },
            "official_skills": whitelist
        }
        success_count += 1
        print(f"  [{idx+1}/{len(jobs)}] {jn} ({fam}): {len(job_skills)} wiki skills -> {len(whitelist)} whitelisted")
        time.sleep(0.1)

    with open(OUTPUT_CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(final_catalog, f, ensure_ascii=False, indent=2)

    print(f"\n[SUCCESS] Saved wiki-grounded catalog with {success_count}/{len(jobs)} jobs to {OUTPUT_CATALOG_PATH}")

if __name__ == '__main__':
    main()

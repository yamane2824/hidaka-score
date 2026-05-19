"""
generate_dashboard.py - ピボット式ダッシュボード生成

種牡馬を固定軸として、母父・性別・毛色・調教師・馬体重・兄姉実績を
任意の軸として組み合わせ集計。
ニックス分析モード（期待値からの乖離表示）も提供。

副産物として、母名辞書（dam_data.json）も生成する。
これは繁殖牝馬スコア化ページ（dam-score.html）で使用する。

データソース: ../data/jvdata.db
出力: ../index.html, ../dam_data.json
"""

import sqlite3
import json
import re
from datetime import date
from pathlib import Path
from collections import defaultdict

DB_PATH = Path(__file__).resolve().parent.parent / 'data' / 'jvdata.db'
OUTPUT_PATH = Path(__file__).resolve().parent.parent / 'index.html'
DAM_DATA_PATH = Path(__file__).resolve().parent.parent / 'dam_data.json'

MIN_SIRE_TOTAL = 10
MIN_DAM_FOALS = 2
RECENT_YEAR_THRESHOLD = 2016
GRADE_STAKES = {'A', 'B', 'C', 'D'}
SEX_LABELS = {'1': '牡', '2': '牝', '3': '騸'}

COAT_LABELS = {
    '01': '栗毛', '02': '栃栗毛', '03': '鹿毛', '04': '黒鹿毛',
    '05': '青鹿毛', '06': '青毛', '07': '芦毛', '11': '白毛',
}

WEIGHT_BINS = [
    (0, 420, '420未満'),
    (420, 440, '420-439'),
    (440, 460, '440-459'),
    (460, 480, '460-479'),
    (480, 500, '480-499'),
    (500, 520, '500-519'),
    (520, 9999, '520以上'),
]


def clean_name(raw_name: str) -> str:
    """馬名の先頭数字prefix（2桁/4桁/10桁等、任意の桁数）を除去"""
    s = (raw_name or '').strip()
    return re.sub(r'^\d+', '', s).strip()


def normalize_name(name: str) -> str:
    """馬名の正規化（空白除去）"""
    if not name:
        return ''
    return name.replace(' ', '').replace('\u3000', '').strip()


def weight_bucket(avg_weight):
    if avg_weight is None:
        return None
    for lo, hi, label in WEIGHT_BINS:
        if lo <= avg_weight < hi:
            return label
    return None


def load_horses(conn):
    rows = conn.execute(
        'SELECT horse_id, horse_name, sex_code, coat_code, sire_name, dam_name, raw FROM horse_master'
    ).fetchall()

    horses = []
    for horse_id, horse_name, sex_code, coat_code, sire_name, dam_name, raw in rows:
        if not raw:
            continue
        b = raw.encode('cp932', errors='replace')
        sire = clean_name(sire_name or '')
        dam_sire_raw = b[388:424].decode('cp932', 'replace').strip()
        dam_sire = clean_name(dam_sire_raw)
        birth_str = b[38:46].decode('cp932', 'replace').strip()
        sex = SEX_LABELS.get(sex_code, '?')
        birth_year = horse_id[:4]
        coat = COAT_LABELS.get(coat_code, None)
        dam = clean_name(dam_name or '')

        horses.append({
            'horse_id': horse_id,
            'horse_name': clean_name(horse_name or ''),
            'horse_name_norm': normalize_name(clean_name(horse_name or '')),
            'sire': sire,
            'dam_sire': dam_sire,
            'dam_name': dam,
            'dam_name_norm': normalize_name(dam),
            'sex': sex,
            'coat': coat,
            'birth_str': birth_str,
            'birth_year': birth_year,
        })
    return horses


def load_trainer_map(conn):
    rows = conn.execute(
        'SELECT trainer_code, trainer_name FROM trainer_master'
    ).fetchall()
    return {code: name.replace('\u3000', ' ').strip() for code, name in rows if name}


def load_race_data(conn, horse_ids):
    if not horse_ids:
        return {}
    print(f'  出走データ取得中... ({len(horse_ids)}頭)')

    CHUNK = 900
    race_data = {}

    for i in range(0, len(horse_ids), CHUNK):
        chunk = horse_ids[i:i + CHUNK]
        placeholders = ','.join('?' * len(chunk))
        sql = f'''
            SELECT r.horse_id, r.finish_pos, r.prize, r.weight, r.trainer_code,
                   rc.race_date, rc.raw
            FROM race_horse r
            JOIN race rc ON r.race_id = rc.race_id
            WHERE r.horse_id IN ({placeholders})
              AND rc.place_code BETWEEN '01' AND '10'
        '''
        for hid, finish_pos, prize, weight, trainer_code, race_date, race_raw in conn.execute(sql, chunk):
            race_data.setdefault(hid, []).append(
                (finish_pos, prize, weight, trainer_code, race_date, race_raw)
            )
    return race_data


def per_horse_basic_stats(horse, races, trainer_map):
    won = 0
    grade_won = 0
    prize_total = 0
    debut_date = None
    weights = []
    trainers = defaultdict(int)
    turf_wins = []
    dirt_wins = []

    for finish_pos, prize, weight, tcode, race_date, race_raw in races:
        try:
            prize_total += int(prize)
        except (ValueError, TypeError):
            pass

        if race_date and (debut_date is None or race_date < debut_date):
            debut_date = race_date

        try:
            w = int(weight)
            if 100 <= w <= 700:
                weights.append(w)
        except (ValueError, TypeError):
            pass

        if tcode and tcode.strip() and tcode != '00000':
            trainers[tcode.strip()] += 1

        if finish_pos == '01':
            won += 1
            if race_raw:
                rb = race_raw.encode('cp932', errors='replace')
                grade = rb[614:615].decode('cp932', 'replace')
                if grade in GRADE_STAKES:
                    grade_won += 1
                try:
                    dist = int(rb[697:701].decode('cp932', 'replace').strip())
                    track = rb[705:707].decode('cp932', 'replace').strip()
                    if track.startswith('1'):
                        turf_wins.append(dist)
                    elif track.startswith('2'):
                        dirt_wins.append(dist)
                except (ValueError, TypeError):
                    pass

    avg_weight = sum(weights) / len(weights) if weights else None
    weight_label = weight_bucket(avg_weight)

    main_trainer_code = max(trainers.items(), key=lambda x: x[1])[0] if trainers else None
    main_trainer = trainer_map.get(main_trainer_code, '') if main_trainer_code else ''
    if not main_trainer:
        main_trainer = None

    debut_age = None
    bs = horse['birth_str']
    if bs.isdigit() and len(bs) == 8 and debut_date:
        try:
            birth = date(int(bs[:4]), int(bs[4:6]), int(bs[6:8]))
            debut = date(int(debut_date[:4]), int(debut_date[4:6]), int(debut_date[6:8]))
            debut_age = (debut - birth).days / 30.4375
        except (ValueError, TypeError):
            pass

    return {
        'has_race_record': len(races) > 0,
        'won': 1 if won > 0 else 0,
        'grade_won': 1 if grade_won > 0 else 0,
        'prize': prize_total,
        'weight_bin': weight_label,
        'trainer': main_trainer,
        'debut_age': debut_age,
        'turf_avg': sum(turf_wins) / len(turf_wins) if turf_wins else None,
        'dirt_avg': sum(dirt_wins) / len(dirt_wins) if dirt_wins else None,
    }


def compute_sibling_info(horses, basic_stats):
    horse_name_to_id = {}
    for h in horses:
        if h['horse_name_norm']:
            horse_name_to_id.setdefault(h['horse_name_norm'], h['horse_id'])

    siblings_by_dam = defaultdict(list)
    for h in horses:
        if h['dam_name_norm']:
            siblings_by_dam[h['dam_name_norm']].append(h)

    result = {}
    for h in horses:
        dam_norm = h['dam_name_norm']
        if not dam_norm:
            result[h['horse_id']] = {
                'sibling_count_bin': 'データなし',
                'sibling_class': 'データなし',
                'sibling_win_rate_bin': 'データなし',
                'dam_has_record': False,
            }
            continue

        dam_horse_id = horse_name_to_id.get(dam_norm)
        dam_has_record = False
        if dam_horse_id:
            ds = basic_stats.get(dam_horse_id)
            if ds and ds['has_race_record']:
                dam_has_record = True

        all_same_dam = siblings_by_dam[dam_norm]
        siblings_with_record = [
            s for s in all_same_dam
            if s['horse_id'] != h['horse_id']
            and basic_stats.get(s['horse_id'], {}).get('has_race_record', False)
        ]

        sib_count = len(siblings_with_record)

        if sib_count == 0:
            if dam_has_record:
                result[h['horse_id']] = {
                    'sibling_count_bin': '0 (初仔)',
                    'sibling_class': '初仔',
                    'sibling_win_rate_bin': '初仔',
                    'dam_has_record': True,
                }
            else:
                result[h['horse_id']] = {
                    'sibling_count_bin': 'データなし',
                    'sibling_class': 'データなし',
                    'sibling_win_rate_bin': 'データなし',
                    'dam_has_record': False,
                }
            continue

        if sib_count <= 2:
            count_bin = '1-2頭'
        elif sib_count <= 4:
            count_bin = '3-4頭'
        else:
            count_bin = '5頭以上'

        sib_grade_won = any(basic_stats[s['horse_id']]['grade_won'] > 0 for s in siblings_with_record)
        sib_any_won = any(basic_stats[s['horse_id']]['won'] > 0 for s in siblings_with_record)
        if sib_grade_won:
            sib_class = '重賞勝ち'
        elif sib_any_won:
            sib_class = '一般勝ち'
        else:
            sib_class = '未勝利'

        sib_won_count = sum(basic_stats[s['horse_id']]['won'] for s in siblings_with_record)
        win_rate = sib_won_count / sib_count
        if win_rate == 0:
            rate_bin = '0%'
        elif win_rate <= 0.25:
            rate_bin = '1-25%'
        elif win_rate <= 0.50:
            rate_bin = '26-50%'
        elif win_rate <= 0.75:
            rate_bin = '51-75%'
        else:
            rate_bin = '76-100%'

        result[h['horse_id']] = {
            'sibling_count_bin': count_bin,
            'sibling_class': sib_class,
            'sibling_win_rate_bin': rate_bin,
            'dam_has_record': dam_has_record,
        }

    return result


def compute_dam_dictionary(horses, basic_stats):
    """
    母名 → 母情報 + 産駒リストの辞書を作成
    JSONサイズ削減のためキー名を短縮:
      n: dam_name, p: dam_prize_man, r: dam_has_record, f: foals
      foals: h=name, s=sire, x=sex, y=year, w=won, g=grade_won, p=prize_man
    """
    horse_name_to_id = {}
    for h in horses:
        if h['horse_name_norm']:
            horse_name_to_id.setdefault(h['horse_name_norm'], h['horse_id'])

    dam_dict = {}
    for h in horses:
        if not h['dam_name_norm']:
            continue
        dam_norm = h['dam_name_norm']

        if dam_norm not in dam_dict:
            dam_horse_id = horse_name_to_id.get(dam_norm)
            dam_prize_man = 0
            dam_has_record = False
            if dam_horse_id:
                ds = basic_stats.get(dam_horse_id)
                if ds:
                    dam_prize_man = ds['prize'] * 100 / 10000
                    dam_has_record = ds['has_race_record']

            dam_dict[dam_norm] = {
                'n': h['dam_name'],
                'p': round(dam_prize_man, 1),
                'r': 1 if dam_has_record else 0,
                'f': [],
            }

        stats = basic_stats.get(h['horse_id'], {})
        dam_dict[dam_norm]['f'].append({
            'h': h['horse_name'],
            's': h['sire'],
            'x': h['sex'],
            'w': stats.get('won', 0),
            'g': stats.get('grade_won', 0),
            'p': round(stats.get('prize', 0) * 100 / 10000, 1),
        })

    return dam_dict


def build_records(horses, basic_stats, sibling_info):
    result_by_sire = defaultdict(list)

    for h in horses:
        sire = h['sire']
        if not sire:
            continue
        if h['sex'] == '?':
            continue
        if not h['dam_sire']:
            continue

        stats = basic_stats.get(h['horse_id'])
        if not stats:
            continue

        # 出走経験のない馬は集計対象外
        if not stats['has_race_record']:
            continue

        sib = sibling_info.get(h['horse_id'], {})

        result_by_sire[sire].append({
            'dam_sire': h['dam_sire'],
            'sex': h['sex'],
            'coat': h['coat'],
            'trainer': stats['trainer'],
            'weight_bin': stats['weight_bin'],
            'sibling_count_bin': sib.get('sibling_count_bin', 'データなし'),
            'sibling_class': sib.get('sibling_class', 'データなし'),
            'sibling_win_rate_bin': sib.get('sibling_win_rate_bin', 'データなし'),
            'year': h['birth_year'],
            'won': stats['won'],
            'grade_won': stats['grade_won'],
            'prize': stats['prize'],
            'debut_age': stats['debut_age'],
            'turf_avg': stats['turf_avg'],
            'dirt_avg': stats['dirt_avg'],
        })

    return result_by_sire


def compute_nicks_baselines(result_by_sire):
    total_count = 0
    total_won = 0
    total_prize = 0
    for sire, records in result_by_sire.items():
        for r in records:
            total_count += 1
            total_won += r['won']
            total_prize += r['prize']
    overall_rate = total_won / total_count if total_count else 0
    overall_prize_man = total_prize * 100 / total_count / 10000 if total_count else 0
    overall = {'rate': overall_rate, 'prize_man': overall_prize_man}

    sire_baselines = {}
    for sire, records in result_by_sire.items():
        n = len(records)
        if n == 0:
            continue
        won_sum = sum(r['won'] for r in records)
        prize_sum = sum(r['prize'] for r in records)
        sire_baselines[sire] = {
            'rate': won_sum / n,
            'prize_man': prize_sum * 100 / n / 10000,
            'total': n,
        }

    dam_sire_stats = defaultdict(lambda: {'n': 0, 'won': 0, 'prize': 0})
    for sire, records in result_by_sire.items():
        for r in records:
            ds = r['dam_sire']
            if not ds:
                continue
            dam_sire_stats[ds]['n'] += 1
            dam_sire_stats[ds]['won'] += r['won']
            dam_sire_stats[ds]['prize'] += r['prize']

    dam_sire_factors = {}
    for ds, s in dam_sire_stats.items():
        if s['n'] < 20:
            continue
        ds_rate = s['won'] / s['n']
        ds_prize_man = s['prize'] * 100 / s['n'] / 10000
        dam_sire_factors[ds] = {
            'rate_factor': ds_rate / overall_rate if overall_rate else 1.0,
            'prize_factor': ds_prize_man / overall_prize_man if overall_prize_man else 1.0,
            'n': s['n'],
        }

    return sire_baselines, dam_sire_factors, overall


def build_payload(result_by_sire, sire_baselines, dam_sire_factors, overall):
    payload = {
        'sires': [],
        'data': {},
        'dam_sire_factors': dam_sire_factors,
        'overall': overall,
    }

    for sire, records in result_by_sire.items():
        total = len(records)
        if total < MIN_SIRE_TOTAL:
            continue

        years = {r['year'] for r in records if r['year'].isdigit()}
        latest_year = max((int(y) for y in years), default=0)
        if latest_year < RECENT_YEAR_THRESHOLD:
            continue

        payload['sires'].append({'name': sire, 'total': total})
        payload['data'][sire] = {
            'total': total,
            'records': records,
            'baseline': sire_baselines.get(sire, {'rate': 0, 'prize_man': 0}),
        }

    payload['sires'].sort(key=lambda x: -x['total'])
    return payload


HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>種牡馬ダッシュボード</title>
<style>
:root {
  --bg: #ffffff; --bg-secondary: #f7f6f2;
  --text: #1a1a1a; --text-secondary: #6a6a6a; --text-tertiary: #999999;
  --border: rgba(0, 0, 0, 0.1); --border-strong: rgba(0, 0, 0, 0.2);
  --accent: #185FA5; --accent-bg: #E6F1FB;
  --pink: #993556; --pink-bg: #FBEAF0;
  --gray: #5F5E5A; --gray-bg: #F1EFE8;
  --green: #3B6D11; --green-bg: #EAF3DE;
  --amber: #854F0B; --amber-bg: #FAEEDA;
  --red: #B83A3A; --red-bg: #FCE4E4;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1a1a1a; --bg-secondary: #252525;
    --text: #e8e8e8; --text-secondary: #a8a8a8; --text-tertiary: #6a6a6a;
    --border: rgba(255, 255, 255, 0.12); --border-strong: rgba(255, 255, 255, 0.25);
    --accent: #5DA1E0; --accent-bg: #1d3a5c;
    --pink: #E693B5; --pink-bg: #4a1f30;
    --gray: #999; --gray-bg: #333;
    --green: #97C459; --green-bg: #1e3a0a;
    --amber: #EF9F27; --amber-bg: #3d2a05;
    --red: #E37B7B; --red-bg: #4a1c1c;
  }
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic", sans-serif;
  background: var(--bg); color: var(--text);
  margin: 0; padding: 0; font-size: 14px; line-height: 1.6;
}
.container { max-width: 1500px; margin: 0 auto; padding: 24px 20px 60px; }
header { border-bottom: 0.5px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }
.nav {
  display: flex; gap: 16px; margin-bottom: 12px;
}
.nav a {
  color: var(--text-secondary); text-decoration: none;
  padding: 6px 12px; border-radius: 6px; font-size: 13px;
}
.nav a:hover { background: var(--bg-secondary); color: var(--text); }
.nav a.active { background: var(--accent-bg); color: var(--accent); font-weight: 500; }
h1 { font-size: 22px; font-weight: 500; margin: 0 0 4px; }
.subtitle { color: var(--text-secondary); font-size: 13px; }
.controls {
  display: flex; flex-wrap: wrap; gap: 12px; align-items: end;
  margin-bottom: 8px; padding: 14px 16px;
  background: var(--bg-secondary); border-radius: 8px;
}
.controls.filters { margin-bottom: 12px; }
.section-label {
  font-size: 11px; color: var(--text-tertiary);
  text-transform: uppercase; letter-spacing: 0.05em;
  margin: 0 8px 0 4px; align-self: center;
}
.control { display: flex; flex-direction: column; gap: 4px; }
.control label { font-size: 12px; color: var(--text-secondary); }
select, input[type="text"], input[type="number"] {
  padding: 6px 10px; border: 0.5px solid var(--border-strong); border-radius: 6px;
  background: var(--bg); color: var(--text); font-size: 13px; font-family: inherit;
}
.input-wrap { position: relative; display: flex; align-items: center; }
.clear-btn {
  position: absolute; right: 4px; background: transparent; border: none;
  color: var(--text-tertiary); font-size: 16px; cursor: pointer; padding: 4px 8px; line-height: 1;
}
.clear-btn:hover { color: var(--text); }
.axis-bar {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin-bottom: 20px; padding: 12px 16px;
  background: var(--bg-secondary); border-radius: 8px;
}
.axis-label { font-size: 12px; color: var(--text-secondary); margin-right: 4px; }
.axis-toggle {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 10px; border: 0.5px solid var(--border-strong); border-radius: 14px;
  background: var(--bg); color: var(--text-secondary);
  font-size: 12px; cursor: pointer; user-select: none; transition: all 0.15s;
}
.axis-toggle:hover { color: var(--text); }
.axis-toggle.active {
  background: var(--accent-bg); color: var(--accent); border-color: var(--accent);
}
.axis-toggle.disabled {
  opacity: 0.35; cursor: not-allowed; text-decoration: line-through;
}
.nicks-toggle {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 5px 10px; border: 0.5px solid var(--border-strong); border-radius: 14px;
  background: var(--bg); color: var(--text-secondary);
  font-size: 12px; cursor: pointer; user-select: none; margin-left: 8px;
}
.nicks-toggle.active { background: var(--pink-bg); color: var(--pink); border-color: var(--pink); }
.nicks-toggle.disabled { opacity: 0.35; cursor: not-allowed; }
.summary {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px; margin-bottom: 20px;
}
.metric { background: var(--bg-secondary); padding: 14px 16px; border-radius: 8px; }
.metric-label { font-size: 12px; color: var(--text-secondary); margin-bottom: 4px; }
.metric-value { font-size: 22px; font-weight: 500; font-variant-numeric: tabular-nums; }
.metric-sub { font-size: 14px; color: var(--text-secondary); font-weight: 400; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
  text-align: left; padding: 10px 12px; font-weight: 500; color: var(--text-secondary);
  border-bottom: 0.5px solid var(--border-strong); white-space: nowrap;
  cursor: pointer; user-select: none;
}
th:hover { color: var(--text); }
th.numeric { text-align: right; }
th.center { text-align: center; }
th .sort-arrow { font-size: 10px; margin-left: 2px; opacity: 0.5; }
th.active .sort-arrow { opacity: 1; color: var(--accent); }
td { padding: 9px 12px; border-bottom: 0.5px solid var(--border); font-variant-numeric: tabular-nums; }
td.numeric { text-align: right; }
td.center { text-align: center; }
.sex-pill {
  display: inline-block; padding: 2px 9px; border-radius: 10px;
  font-size: 11px; font-weight: 500;
}
.sex-pill.male { background: var(--accent-bg); color: var(--accent); }
.sex-pill.female { background: var(--pink-bg); color: var(--pink); }
.sex-pill.gelding { background: var(--gray-bg); color: var(--gray); }
.track-pill {
  display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 11px; margin: 1px;
}
.track-pill.turf { background: var(--green-bg); color: var(--green); }
.track-pill.dirt { background: var(--amber-bg); color: var(--amber); }
.nicks-pill {
  display: inline-block; padding: 1px 7px; border-radius: 4px;
  font-size: 11px; font-weight: 500; font-variant-numeric: tabular-nums;
}
.nicks-pill.strong { background: var(--green-bg); color: var(--green); }
.nicks-pill.mid { background: var(--accent-bg); color: var(--accent); }
.nicks-pill.flat { color: var(--text-tertiary); }
.nicks-pill.weak { background: var(--amber-bg); color: var(--amber); }
.nicks-pill.very-weak { background: var(--red-bg); color: var(--red); }
.row-low { opacity: 0.45; }
.row-mid { opacity: 0.7; }
.bar-cell { position: relative; }
.bar-bg { position: absolute; inset: 0; border-radius: 3px; z-index: 0; }
.bar-bg.prize { background: var(--accent-bg); }
.bar-bg.grade { background: var(--pink-bg); }
.bar-cell > span { position: relative; z-index: 1; }
.empty-msg { padding: 40px 20px; text-align: center; color: var(--text-tertiary); }
.note {
  margin-top: 24px; padding: 14px 16px; background: var(--bg-secondary);
  border-radius: 8px; font-size: 12px; color: var(--text-secondary); line-height: 1.7;
}
.note strong { color: var(--text); font-weight: 500; }
.baseline-info { font-size: 11px; color: var(--text-tertiary); margin-left: 8px; }
@media (max-width: 600px) {
  .container { padding: 16px 12px; }
  table { font-size: 12px; }
  th, td { padding: 7px 8px; }
}
</style>
</head>
<body>
<div class="container">
<header>
  <div class="nav">
    <a href="/" class="active">種牡馬ダッシュボード</a>
    <a href="/dam-score.html">繁殖牝馬スコア</a>
  </div>
  <h1>種牡馬ダッシュボード</h1>
  <div class="subtitle">JRA平地レース／horse_master登録の競走馬／生年__YEAR_MIN__〜__YEAR_MAX__</div>
</header>

<div class="controls">
  <span class="section-label">対象</span>
  <div class="control">
    <label>種牡馬</label>
    <div id="sire-area"></div>
  </div>
  <div class="control">
    <label>最小頭数</label>
    <select id="min-count">
      <option value="3">3頭以上</option>
      <option value="5" selected>5頭以上</option>
      <option value="10">10頭以上</option>
      <option value="20">20頭以上</option>
    </select>
  </div>
  <div class="control">
    <label>生年から</label>
    <input type="number" id="year-min" min="2017" max="2024" value="2017" style="width:80px;">
  </div>
  <div class="control">
    <label>まで</label>
    <input type="number" id="year-max" min="2017" max="2024" value="2024" style="width:80px;">
  </div>
</div>

<div class="controls filters">
  <span class="section-label">絞り込み</span>
  <div class="control">
    <label>母父</label>
    <div class="input-wrap">
      <input type="text" id="filter-dam_sire" list="damsire-list" placeholder="部分一致" style="min-width: 160px; padding-right: 28px;">
      <button class="clear-btn" data-clear="dam_sire">×</button>
      <datalist id="damsire-list"></datalist>
    </div>
  </div>
  <div class="control">
    <label>性別</label>
    <select id="filter-sex">
      <option value="">すべて</option>
      <option value="牡">牡</option>
      <option value="牝">牝</option>
      <option value="騸">騸</option>
    </select>
  </div>
  <div class="control">
    <label>毛色</label>
    <select id="filter-coat">
      <option value="">すべて</option>
      <option value="鹿毛">鹿毛</option>
      <option value="黒鹿毛">黒鹿毛</option>
      <option value="栗毛">栗毛</option>
      <option value="芦毛">芦毛</option>
      <option value="青鹿毛">青鹿毛</option>
      <option value="栃栗毛">栃栗毛</option>
      <option value="青毛">青毛</option>
      <option value="白毛">白毛</option>
    </select>
  </div>
  <div class="control">
    <label>調教師</label>
    <div class="input-wrap">
      <input type="text" id="filter-trainer" list="trainer-list" placeholder="部分一致" style="min-width: 140px; padding-right: 28px;">
      <button class="clear-btn" data-clear="trainer">×</button>
      <datalist id="trainer-list"></datalist>
    </div>
  </div>
  <div class="control">
    <label>馬体重</label>
    <select id="filter-weight_bin">
      <option value="">すべて</option>
      <option value="420未満">420未満</option>
      <option value="420-439">420-439</option>
      <option value="440-459">440-459</option>
      <option value="460-479">460-479</option>
      <option value="480-499">480-499</option>
      <option value="500-519">500-519</option>
      <option value="520以上">520以上</option>
    </select>
  </div>
</div>

<div class="axis-bar">
  <span class="axis-label">集計軸:</span>
  <span class="axis-toggle active" data-axis="dam_sire">母父</span>
  <span class="axis-toggle active" data-axis="sex">性別</span>
  <span class="axis-toggle" data-axis="coat">毛色</span>
  <span class="axis-toggle" data-axis="trainer">調教師</span>
  <span class="axis-toggle" data-axis="weight_bin">馬体重</span>
  <span class="axis-toggle" data-axis="sibling_class">兄姉最高クラス</span>
  <span class="axis-toggle" data-axis="sibling_win_rate_bin">兄姉勝上率</span>
  <span class="axis-toggle" data-axis="sibling_count_bin">兄姉頭数</span>
  <span class="nicks-toggle" id="nicks-toggle" title="母父を軸にしたときだけ有効">ニックス分析</span>
  <span class="baseline-info" id="baseline-info"></span>
</div>

<div class="summary" id="summary"></div>

<div style="overflow-x: auto;">
<table id="data-table">
  <thead><tr id="header-row"></tr></thead>
  <tbody id="data-body"></tbody>
</table>
</div>

<div class="empty-msg" id="empty-msg" style="display:none;">該当データなし</div>
</div>

<script>
const PAYLOAD = __PAYLOAD__;

const AXIS_DEFS = {
  dam_sire: { label: '母父' },
  sex: { label: '性別' },
  coat: { label: '毛色' },
  trainer: { label: '調教師' },
  weight_bin: { label: '馬体重' },
  sibling_class: { label: '兄姉最高クラス' },
  sibling_win_rate_bin: { label: '兄姉勝上率' },
  sibling_count_bin: { label: '兄姉頭数' },
};

const WEIGHT_ORDER = ['420未満','420-439','440-459','460-479','480-499','500-519','520以上'];
const COAT_ORDER = ['鹿毛','黒鹿毛','栗毛','芦毛','青鹿毛','栃栗毛','青毛','白毛'];
const SIB_CLASS_ORDER = ['重賞勝ち','一般勝ち','未勝利','初仔','データなし'];
const SIB_RATE_ORDER = ['76-100%','51-75%','26-50%','1-25%','0%','初仔','データなし'];
const SIB_COUNT_ORDER = ['5頭以上','3-4頭','1-2頭','0 (初仔)','データなし'];

const state = {
  sire: null,
  axes: ['dam_sire', 'sex'],
  filters: { dam_sire: '', sex: '', coat: '', trainer: '', weight_bin: '' },
  minCount: 5,
  yearMin: 2017,
  yearMax: 2024,
  sortKey: 'count',
  sortDir: 'desc',
  sireMode: 'select',
  nicks: false,
};

function fmtPrize(man) {
  if (man == null || isNaN(man)) return '-';
  if (man >= 10000) return (man / 10000).toFixed(2) + '億円';
  return Math.round(man).toLocaleString() + '万円';
}
function fmtMonths(m) {
  if (m == null || isNaN(m)) return '-';
  return m.toFixed(1) + 'ヶ月';
}
function fmtDist(d) {
  if (d == null || isNaN(d)) return '-';
  return Math.round(d).toLocaleString() + 'm';
}
function reliabilityClass(count) {
  if (count >= 10) return '';
  if (count >= 5) return 'row-mid';
  return 'row-low';
}

function applyFilters(records) {
  const f = state.filters;
  return records.filter(r => {
    if (state.yearMin && parseInt(r.year, 10) < state.yearMin) return false;
    if (state.yearMax && parseInt(r.year, 10) > state.yearMax) return false;
    if (f.dam_sire && (!r.dam_sire || !r.dam_sire.toLowerCase().includes(f.dam_sire.toLowerCase()))) return false;
    if (f.sex && r.sex !== f.sex) return false;
    if (f.coat && r.coat !== f.coat) return false;
    if (f.trainer && (!r.trainer || !r.trainer.toLowerCase().includes(f.trainer.toLowerCase()))) return false;
    if (f.weight_bin && r.weight_bin !== f.weight_bin) return false;
    return true;
  });
}

function effectiveAxes() {
  const f = state.filters;
  const filtered = new Set();
  if (f.dam_sire) filtered.add('dam_sire');
  if (f.sex) filtered.add('sex');
  if (f.coat) filtered.add('coat');
  if (f.trainer) filtered.add('trainer');
  if (f.weight_bin) filtered.add('weight_bin');
  let axes = state.axes.filter(a => !filtered.has(a));
  if (axes.length === 0) {
    const fallback = ['dam_sire','sex','coat','trainer','weight_bin','sibling_class','sibling_win_rate_bin','sibling_count_bin'].find(a => !filtered.has(a));
    if (fallback) axes = [fallback];
  }
  return axes;
}

function nicksEnabled() {
  return state.nicks && effectiveAxes().includes('dam_sire');
}

function aggregate(records) {
  const axes = effectiveAxes();
  const groups = new Map();
  for (const r of records) {
    const keyParts = axes.map(a => r[a] == null ? '(不明)' : r[a]);
    const key = keyParts.join('|');
    if (!groups.has(key)) {
      const entry = { count: 0, won: 0, grade_won: 0, prize_sum: 0,
        debut_sum: 0, debut_n: 0,
        turf_sum: 0, turf_n: 0, dirt_sum: 0, dirt_n: 0 };
      axes.forEach((a, i) => entry[a] = keyParts[i]);
      groups.set(key, entry);
    }
    const g = groups.get(key);
    g.count++;
    g.won += r.won;
    g.grade_won += r.grade_won;
    g.prize_sum += r.prize;
    if (r.debut_age != null) { g.debut_sum += r.debut_age; g.debut_n++; }
    if (r.turf_avg != null) { g.turf_sum += r.turf_avg; g.turf_n++; }
    if (r.dirt_avg != null) { g.dirt_sum += r.dirt_avg; g.dirt_n++; }
  }

  const sireBase = PAYLOAD.data[state.sire].baseline;
  const dsFactors = PAYLOAD.dam_sire_factors;

  const result = [];
  for (const g of groups.values()) {
    if (g.count < state.minCount) continue;
    g.rate = g.count > 0 ? g.won / g.count * 100 : 0;
    g.grade_rate = g.count > 0 ? g.grade_won / g.count * 100 : 0;
    g.avg_prize_man = g.count > 0 ? g.prize_sum * 100 / g.count / 10000 : 0;
    g.debut_avg = g.debut_n > 0 ? g.debut_sum / g.debut_n : null;
    g.turf_avg = g.turf_n > 0 ? g.turf_sum / g.turf_n : null;
    g.dirt_avg = g.dirt_n > 0 ? g.dirt_sum / g.dirt_n : null;

    if (nicksEnabled() && g.dam_sire && g.dam_sire !== '(不明)') {
      const f = dsFactors[g.dam_sire];
      if (f) {
        const expectedRate = sireBase.rate * f.rate_factor * 100;
        const expectedPrizeMan = sireBase.prize_man * f.prize_factor;
        g.expected_rate = expectedRate;
        g.expected_prize_man = expectedPrizeMan;
        g.rate_dev = expectedRate > 0 ? g.rate / expectedRate : null;
        g.prize_dev = expectedPrizeMan > 0 ? g.avg_prize_man / expectedPrizeMan : null;
      } else {
        g.expected_rate = null; g.expected_prize_man = null;
        g.rate_dev = null; g.prize_dev = null;
      }
    }
    result.push(g);
  }
  return result;
}

function sortRows(rows) {
  const k = state.sortKey;
  const dir = state.sortDir === 'asc' ? 1 : -1;
  rows.sort((a, b) => {
    let av = a[k]; let bv = b[k];
    if (k === 'weight_bin') {
      av = WEIGHT_ORDER.indexOf(av || ''); bv = WEIGHT_ORDER.indexOf(bv || '');
      return (av - bv) * dir;
    }
    if (k === 'coat') {
      av = COAT_ORDER.indexOf(av || ''); bv = COAT_ORDER.indexOf(bv || '');
      return (av - bv) * dir;
    }
    if (k === 'sibling_class') {
      av = SIB_CLASS_ORDER.indexOf(av || ''); bv = SIB_CLASS_ORDER.indexOf(bv || '');
      return (av - bv) * dir;
    }
    if (k === 'sibling_win_rate_bin') {
      av = SIB_RATE_ORDER.indexOf(av || ''); bv = SIB_RATE_ORDER.indexOf(bv || '');
      return (av - bv) * dir;
    }
    if (k === 'sibling_count_bin') {
      av = SIB_COUNT_ORDER.indexOf(av || ''); bv = SIB_COUNT_ORDER.indexOf(bv || '');
      return (av - bv) * dir;
    }
    if (av == null) av = -Infinity;
    if (bv == null) bv = -Infinity;
    if (typeof av === 'string') return av.localeCompare(bv, 'ja') * dir;
    return (av - bv) * dir;
  });
  return rows;
}

function renderHeader() {
  const axes = effectiveAxes();
  const tr = document.getElementById('header-row');
  let html = '';
  for (const ax of axes) {
    const def = AXIS_DEFS[ax];
    const align = ax === 'sex' ? 'center' : '';
    html += `<th class="${align}" data-sort="${ax}">${def.label}<span class="sort-arrow">↕</span></th>`;
  }
  html += `
    <th class="numeric" data-sort="count">頭数<span class="sort-arrow">↕</span></th>
    <th class="numeric" data-sort="rate">勝上率<span class="sort-arrow">↕</span></th>`;
  if (nicksEnabled()) {
    html += `
      <th class="numeric" data-sort="expected_rate">期待勝上率<span class="sort-arrow">↕</span></th>
      <th class="numeric" data-sort="rate_dev">勝率乖離<span class="sort-arrow">↕</span></th>`;
  }
  html += `
    <th class="numeric" data-sort="grade_rate">重賞率<span class="sort-arrow">↕</span></th>
    <th class="numeric" data-sort="avg_prize_man">平均賞金<span class="sort-arrow">↕</span></th>`;
  if (nicksEnabled()) {
    html += `
      <th class="numeric" data-sort="expected_prize_man">期待賞金<span class="sort-arrow">↕</span></th>
      <th class="numeric" data-sort="prize_dev">賞金乖離<span class="sort-arrow">↕</span></th>`;
  }
  html += `
    <th class="numeric" data-sort="debut_avg">デビュー月齢<span class="sort-arrow">↕</span></th>
    <th class="numeric" data-sort="turf_avg">芝平均距離<span class="sort-arrow">↕</span></th>
    <th class="numeric" data-sort="dirt_avg">ダ平均距離<span class="sort-arrow">↕</span></th>
  `;
  tr.innerHTML = html;
  tr.querySelectorAll('th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
      const k = th.dataset.sort;
      if (state.sortKey === k) {
        state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        state.sortKey = k;
        const isText = ['dam_sire','sex','coat','trainer','weight_bin','sibling_class','sibling_win_rate_bin','sibling_count_bin'].includes(k);
        state.sortDir = isText ? 'asc' : 'desc';
      }
      renderTable();
    });
  });
}

function renderSummary(filteredRecords) {
  const total = filteredRecords.length;
  const won = filteredRecords.reduce((a, r) => a + r.won, 0);
  const gwon = filteredRecords.reduce((a, r) => a + r.grade_won, 0);
  document.getElementById('summary').innerHTML = `
    <div class="metric">
      <div class="metric-label">対象頭数</div>
      <div class="metric-value">${total.toLocaleString()}</div>
    </div>
    <div class="metric">
      <div class="metric-label">勝ち上がり</div>
      <div class="metric-value">${won.toLocaleString()} <span class="metric-sub">(${total ? (won/total*100).toFixed(1) : 0}%)</span></div>
    </div>
    <div class="metric">
      <div class="metric-label">重賞勝ち頭数</div>
      <div class="metric-value">${gwon.toLocaleString()} <span class="metric-sub">(${total ? (gwon/total*100).toFixed(2) : 0}%)</span></div>
    </div>
  `;
}

function renderAxisToggles() {
  const f = state.filters;
  document.querySelectorAll('.axis-toggle').forEach(el => {
    const ax = el.dataset.axis;
    const isFiltered = !!f[ax];
    el.classList.toggle('disabled', isFiltered);
    el.classList.toggle('active', !isFiltered && state.axes.includes(ax));
  });
  const nicksBtn = document.getElementById('nicks-toggle');
  const hasDamSireAxis = effectiveAxes().includes('dam_sire');
  if (!hasDamSireAxis) {
    nicksBtn.classList.add('disabled');
    nicksBtn.classList.remove('active');
  } else {
    nicksBtn.classList.remove('disabled');
    nicksBtn.classList.toggle('active', state.nicks);
  }
  const sireBase = PAYLOAD.data[state.sire] ? PAYLOAD.data[state.sire].baseline : null;
  const infoEl = document.getElementById('baseline-info');
  if (sireBase && nicksEnabled()) {
    infoEl.textContent = `（基準: 全産駒平均勝上率 ${(sireBase.rate*100).toFixed(1)}% / 平均賞金 ${fmtPrize(sireBase.prize_man)}）`;
  } else {
    infoEl.textContent = '';
  }
}

function nicksPillClass(dev) {
  if (dev == null) return 'flat';
  if (dev >= 1.30) return 'strong';
  if (dev >= 1.10) return 'mid';
  if (dev <= 0.70) return 'very-weak';
  if (dev <= 0.90) return 'weak';
  return 'flat';
}
function nicksPillText(dev) {
  if (dev == null) return '-';
  let arrow = '→';
  if (dev >= 1.30) arrow = '⬆';
  else if (dev >= 1.10) arrow = '↑';
  else if (dev <= 0.70) arrow = '⬇';
  else if (dev <= 0.90) arrow = '↓';
  return `${dev.toFixed(2)}x ${arrow}`;
}

function renderTable() {
  const sireData = PAYLOAD.data[state.sire];
  if (!sireData) return;
  const filtered = applyFilters(sireData.records);
  renderHeader();
  renderSummary(filtered);
  renderAxisToggles();
  let rows = aggregate(filtered);
  const axes = effectiveAxes();
  const validSortKeys = new Set([...axes, 'count', 'rate', 'grade_rate', 'avg_prize_man', 'debut_avg', 'turf_avg', 'dirt_avg', 'expected_rate', 'rate_dev', 'expected_prize_man', 'prize_dev']);
  if (!validSortKeys.has(state.sortKey)) {
    state.sortKey = 'count'; state.sortDir = 'desc';
  }
  rows = sortRows(rows);
  const tbody = document.getElementById('data-body');
  const empty = document.getElementById('empty-msg');
  if (rows.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';
  const maxPrize = Math.max(...rows.map(r => r.avg_prize_man || 0), 1);
  const maxGrade = Math.max(...rows.map(r => r.grade_rate || 0), 1);
  tbody.innerHTML = rows.map(r => {
    const opClass = reliabilityClass(r.count);
    const prizeBar = Math.min((r.avg_prize_man || 0) / maxPrize * 100, 100);
    const gradeBar = Math.min((r.grade_rate || 0) / maxGrade * 100, 100);
    let axisCells = '';
    for (const ax of axes) {
      const v = r[ax] || '-';
      if (ax === 'sex') {
        const sc = v === '牡' ? 'male' : v === '牝' ? 'female' : 'gelding';
        axisCells += `<td class="center"><span class="sex-pill ${sc}">${v}</span></td>`;
      } else {
        axisCells += `<td>${v}</td>`;
      }
    }
    let nicksRateCells = '';
    let nicksPrizeCells = '';
    if (nicksEnabled()) {
      const exr = r.expected_rate != null ? r.expected_rate.toFixed(1) + '%' : '-';
      const exp = r.expected_prize_man != null ? fmtPrize(r.expected_prize_man) : '-';
      const rateDev = r.rate_dev;
      const prizeDev = r.prize_dev;
      nicksRateCells = `
        <td class="numeric" style="color:var(--text-tertiary)">${exr}</td>
        <td class="numeric"><span class="nicks-pill ${nicksPillClass(rateDev)}">${nicksPillText(rateDev)}</span></td>
      `;
      nicksPrizeCells = `
        <td class="numeric" style="color:var(--text-tertiary)">${exp}</td>
        <td class="numeric"><span class="nicks-pill ${nicksPillClass(prizeDev)}">${nicksPillText(prizeDev)}</span></td>
      `;
    }
    const turfCell = r.turf_avg != null
      ? `<span class="track-pill turf">芝</span> ${fmtDist(r.turf_avg)}` : '-';
    const dirtCell = r.dirt_avg != null
      ? `<span class="track-pill dirt">ダ</span> ${fmtDist(r.dirt_avg)}` : '-';
    return `
      <tr class="${opClass}">
        ${axisCells}
        <td class="numeric">${r.count}</td>
        <td class="numeric">${r.rate.toFixed(1)}%</td>
        ${nicksRateCells}
        <td class="numeric bar-cell">
          <div class="bar-bg grade" style="width:${gradeBar}%;"></div>
          <span>${r.grade_rate.toFixed(1)}%</span>
        </td>
        <td class="numeric bar-cell">
          <div class="bar-bg prize" style="width:${prizeBar}%;"></div>
          <span>${fmtPrize(r.avg_prize_man)}</span>
        </td>
        ${nicksPrizeCells}
        <td class="numeric">${fmtMonths(r.debut_avg)}</td>
        <td class="numeric">${turfCell}</td>
        <td class="numeric">${dirtCell}</td>
      </tr>
    `;
  }).join('');
  document.querySelectorAll('th[data-sort]').forEach(th => {
    th.classList.toggle('active', th.dataset.sort === state.sortKey);
    const arrow = th.querySelector('.sort-arrow');
    if (arrow) {
      arrow.textContent = th.dataset.sort === state.sortKey
        ? (state.sortDir === 'asc' ? '↑' : '↓') : '↕';
    }
  });
}

function renderSireSelect() {
  const area = document.getElementById('sire-area');
  area.innerHTML = `
    <div style="display: flex; align-items: center; gap: 4px;">
      <select id="sire-select" style="min-width: 220px;"></select>
      <button class="clear-btn" id="sire-to-input" style="position: static; padding: 4px 8px;" title="検索モードに切り替え">×</button>
    </div>
  `;
  const sel = document.getElementById('sire-select');
  PAYLOAD.sires.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.name;
    opt.textContent = `${s.name}  (${s.total}頭)`;
    sel.appendChild(opt);
  });
  sel.value = state.sire;
  sel.addEventListener('change', e => { state.sire = e.target.value; renderTable(); });
  document.getElementById('sire-to-input').addEventListener('click', () => {
    state.sireMode = 'input'; renderSireInput(true);
  });
}

function renderSireInput(autofocus) {
  const area = document.getElementById('sire-area');
  area.innerHTML = `
    <div class="input-wrap">
      <input type="text" id="sire-input" list="sire-list" placeholder="入力..." style="min-width: 220px; padding-right: 28px;">
      <button class="clear-btn" id="sire-back-select">▼</button>
      <datalist id="sire-list"></datalist>
    </div>
  `;
  const dl = document.getElementById('sire-list');
  PAYLOAD.sires.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.name;
    opt.label = `${s.name}  (${s.total}頭)`;
    dl.appendChild(opt);
  });
  const inp = document.getElementById('sire-input');
  inp.value = '';
  if (autofocus) setTimeout(() => inp.focus(), 0);
  inp.addEventListener('input', e => {
    const v = e.target.value.trim();
    if (PAYLOAD.data[v]) { state.sire = v; renderTable(); }
  });
  inp.addEventListener('blur', e => {
    const v = e.target.value.trim();
    if (PAYLOAD.data[v]) { state.sireMode = 'select'; renderSireSelect(); }
  });
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      const v = e.target.value.trim();
      if (PAYLOAD.data[v]) {
        state.sire = v; state.sireMode = 'select';
        renderSireSelect(); renderTable();
      }
    }
  });
  document.getElementById('sire-back-select').addEventListener('click', () => {
    state.sireMode = 'select'; renderSireSelect();
  });
}

function init() {
  state.sire = PAYLOAD.sires[0].name;
  renderSireSelect();
  const damsireSet = new Set();
  const trainerSet = new Set();
  for (const sn in PAYLOAD.data) {
    PAYLOAD.data[sn].records.forEach(r => {
      if (r.dam_sire) damsireSet.add(r.dam_sire);
      if (r.trainer) trainerSet.add(r.trainer);
    });
  }
  const damsireDl = document.getElementById('damsire-list');
  Array.from(damsireSet).sort().forEach(name => {
    const opt = document.createElement('option');
    opt.value = name;
    damsireDl.appendChild(opt);
  });
  const trainerDl = document.getElementById('trainer-list');
  Array.from(trainerSet).sort().forEach(name => {
    const opt = document.createElement('option');
    opt.value = name;
    trainerDl.appendChild(opt);
  });
  ['dam_sire', 'trainer'].forEach(key => {
    const inp = document.getElementById('filter-' + key);
    inp.addEventListener('focus', e => e.target.select());
    inp.addEventListener('input', e => { state.filters[key] = e.target.value; renderTable(); });
  });
  ['sex', 'coat', 'weight_bin'].forEach(key => {
    const sel = document.getElementById('filter-' + key);
    sel.addEventListener('change', e => { state.filters[key] = e.target.value; renderTable(); });
  });
  document.querySelectorAll('.clear-btn[data-clear]').forEach(btn => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.clear;
      state.filters[key] = '';
      const inp = document.getElementById('filter-' + key);
      if (inp) inp.value = '';
      renderTable();
    });
  });
  document.querySelectorAll('.axis-toggle').forEach(el => {
    el.addEventListener('click', () => {
      if (el.classList.contains('disabled')) return;
      const ax = el.dataset.axis;
      const i = state.axes.indexOf(ax);
      if (i >= 0) {
        if (state.axes.length === 1) return;
        state.axes.splice(i, 1);
      } else {
        state.axes.push(ax);
      }
      renderTable();
    });
  });
  document.getElementById('nicks-toggle').addEventListener('click', () => {
    const btn = document.getElementById('nicks-toggle');
    if (btn.classList.contains('disabled')) return;
    state.nicks = !state.nicks;
    renderTable();
  });
  document.getElementById('min-count').addEventListener('change', e => { state.minCount = parseInt(e.target.value, 10); renderTable(); });
  document.getElementById('year-min').addEventListener('input', e => { state.yearMin = parseInt(e.target.value, 10) || 2017; renderTable(); });
  document.getElementById('year-max').addEventListener('input', e => { state.yearMax = parseInt(e.target.value, 10) || 2024; renderTable(); });
  renderTable();
}

init();
</script>
</body>
</html>
'''


def main():
    print(f'DB: {DB_PATH}')
    print(f'OUTPUT: {OUTPUT_PATH}')
    print(f'DAM_DATA: {DAM_DATA_PATH}')

    conn = sqlite3.connect(str(DB_PATH))

    print('競走馬マスタを読み込み中...')
    horses = load_horses(conn)
    print(f'  {len(horses)}頭')

    print('調教師マスタ読み込み中...')
    trainer_map = load_trainer_map(conn)
    print(f'  {len(trainer_map)}人')

    horse_ids = [h['horse_id'] for h in horses]
    race_data = load_race_data(conn, horse_ids)
    print(f'  出走データ取得済み: {len(race_data)}頭分')

    print('馬ごとの基本集計中...')
    basic_stats = {}
    for h in horses:
        races = race_data.get(h['horse_id'], [])
        basic_stats[h['horse_id']] = per_horse_basic_stats(h, races, trainer_map)
    print(f'  基本集計完了: {len(basic_stats)}頭')

    print('兄姉情報を計算中...')
    sibling_info = compute_sibling_info(horses, basic_stats)
    cat_counts = defaultdict(int)
    for info in sibling_info.values():
        cat_counts[info['sibling_class']] += 1
    for cat, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f'  兄姉最高クラス [{cat}]: {n}')

    print('母名辞書を構築中...')
    dam_dict = compute_dam_dictionary(horses, basic_stats)
    print(f'  ユニーク母数: {len(dam_dict)}')

    print('レコード構築中（出走経験のある馬のみ）...')
    result_by_sire = build_records(horses, basic_stats, sibling_info)
    print(f'  種牡馬数: {len(result_by_sire)}')

    print('調教師フィルタ適用中（種牡馬ごとに3頭以上のみ表示）...')
    for sire, records in result_by_sire.items():
        trainer_counts = defaultdict(int)
        for r in records:
            if r['trainer']:
                trainer_counts[r['trainer']] += 1
        for r in records:
            if r['trainer'] and trainer_counts[r['trainer']] < 3:
                r['trainer'] = None

    print('ニックス分析のベースライン計算中...')
    sire_baselines, dam_sire_factors, overall = compute_nicks_baselines(result_by_sire)
    print(f'  全体平均勝上率: {overall["rate"]*100:.1f}%')
    print(f'  全体平均賞金: {overall["prize_man"]:.0f}万円')
    print(f'  母父係数算出済み: {len(dam_sire_factors)}件')

    print(f'ペイロード構築中（種牡馬産駒{MIN_SIRE_TOTAL}頭以上）...')
    payload = build_payload(result_by_sire, sire_baselines, dam_sire_factors, overall)
    print(f'  対象種牡馬: {len(payload["sires"])}')

    if horses:
        years = [int(h['birth_year']) for h in horses if h['birth_year'].isdigit()]
        year_min, year_max = min(years), max(years)
    else:
        year_min, year_max = 2017, 2024

    print('HTML生成中...')
    html = HTML_TEMPLATE.replace('__PAYLOAD__', json.dumps(payload, ensure_ascii=False))
    html = html.replace('__YEAR_MIN__', str(year_min))
    html = html.replace('__YEAR_MAX__', str(year_max))
    OUTPUT_PATH.write_text(html, encoding='utf-8')
    print(f'完了: {OUTPUT_PATH}')
    print(f'  ファイルサイズ: {OUTPUT_PATH.stat().st_size / 1024:.1f} KB')

    print(f'母名辞書をJSONとして保存中（産駒{MIN_DAM_FOALS}頭以上 または 母出走経験あり）...')
    filtered_dam_dict = {
        k: v for k, v in dam_dict.items()
        if len(v['f']) >= MIN_DAM_FOALS or v['r'] == 1
    }
    print(f'  対象母: {len(filtered_dam_dict)}件')
    with open(DAM_DATA_PATH, 'w', encoding='utf-8') as f:
        f.write('{')
        first = True
        for k, v in filtered_dam_dict.items():
            if not first:
                f.write(',')
            first = False
            f.write(json.dumps(k, ensure_ascii=False))
            f.write(':')
            f.write(json.dumps(v, ensure_ascii=False))
        f.write('}')
    print(f'完了: {DAM_DATA_PATH}')
    print(f'  ファイルサイズ: {DAM_DATA_PATH.stat().st_size / 1024:.1f} KB')

    conn.close()


if __name__ == '__main__':
    main()
"""
JRAのレース結果ページの読み取りと、収得賞金・クラスの計算。

収得賞金は競馬番組 一般事項 Ⅰの4の(3)（2026年度）による。
  ・平地競走と障害競走は別々に計算する
  ・第1着と、重賞競走の第2着に算入する（10万円未満の端数は切り捨て）
  ・2021年6月4日以前の競走は従前の取扱い（第1着本賞金・重賞第2着本賞金の半額。新馬・未勝利の第1着は400万円）
クラスは収得賞金で決まる（降級制度は2019年夏季競馬から廃止）
  1勝クラス：500万円以下 ／ 2勝クラス：1,000万円以下 ／ 3勝クラス：1,600万円以下 ／ オープン：1,600万円超
"""
import re
from datetime import date

from bs4 import BeautifulSoup

JRA_RESULT_URL = "https://www.jra.go.jp/JRADB/accessS.html?CNAME="

# 2021年6月4日以前は従前の収得賞金の取扱い
NEW_SHUTOKU_RULE_FROM = date(2021, 6, 5)

_GRADE_ALIASES = [
    ('G1', ('GⅠ', 'G1', 'JpnⅠ', 'J・GⅠ', 'J・G1')),
    ('G2', ('GⅡ', 'G2', 'JpnⅡ', 'J・GⅡ', 'J・G2')),
    ('G3', ('GⅢ', 'G3', 'JpnⅢ', 'J・GⅢ', 'J・G3')),
    ('L', ('L', 'リステッド')),
]


def _text(el):
    return re.sub(r'\s+', ' ', el.get_text(' ', strip=True)).strip() if el else ''


def _soup(html_or_soup):
    if isinstance(html_or_soup, BeautifulSoup):
        return html_or_soup
    return BeautifulSoup(html_or_soup or '', 'html.parser')


def normalize_grade(text):
    """「GⅡ」「J・GⅠ」「リステッド」などを G1／G2／G3／L にそろえる（該当しなければ空文字）"""
    text = (text or '').strip()
    for code, aliases in _GRADE_ALIASES:
        if any(text == a or text.endswith(a) for a in aliases):
            return code
    return ''


def race_id_from_cname(cname):
    """CNAME（pw01sde1005202604020520261004/E2）から、照合用の「/」より前の部分を返す"""
    m = re.search(r'(pw01sde\d+)', cname or '')
    return m.group(1) if m else ''


def horse_id_from_url(url):
    """馬のページのURL（…CNAME=pw01dud102024101851/0F）から馬ID（2024101851）を取り出す"""
    m = re.search(r'pw01dud\d*?(\d{10})(?:/|$)', url or '')
    return m.group(1) if m else ''


VENUES = {'01': '札幌', '02': '函館', '03': '福島', '04': '新潟', '05': '東京',
          '06': '中山', '07': '中京', '08': '京都', '09': '阪神', '10': '小倉'}

_RACE_ID_RE = re.compile(r'pw01sde\d\d(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})(\d{8})')


def parse_race_id(race_id):
    """レースID（pw01sde10 場2桁 年4桁 回2桁 日2桁 R2桁 年月日8桁）を分解する（読み取れなければ None）"""
    m = _RACE_ID_RE.search(race_id or '')
    if not m:
        return None
    ymd = m.group(6)
    try:
        race_date = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
    except ValueError:
        return None
    return {'venue': VENUES.get(m.group(1), m.group(1)), 'kai': int(m.group(3)), 'nichi': int(m.group(4)),
            'num': int(m.group(5)), 'date': race_date}


def day_key(race_id):
    """同じ開催日・同じ競馬場のレースに共通する部分（レース番号を除いたもの）"""
    m = _RACE_ID_RE.search(race_id or '')
    return f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}{m.group(6)}" if m else ''


def day_label(race_id):
    info = parse_race_id(race_id)
    if not info:
        return race_id
    d = info['date']
    return f"{d.year}年{d.month}月{d.day}日 {info['kai']}回{info['venue']}{info['nichi']}日"


def result_urls_by_day(html_or_soup):
    """ページ内のレース結果へのリンクを開催日（競馬場ごと）にまとめる。{day_key: [URL, ...]}
    開催選択のページには各開催日の11Rへ、レース結果のページには同じ日の全レースへのリンクがある"""
    days = {}
    for url in parse_race_list_page(html_or_soup):
        key = day_key(url)
        if key:
            days.setdefault(key, []).append(url)
    return days


def is_result_page(soup):
    soup = _soup(soup)
    return soup.select_one('div.race_header') is not None and soup.select_one('table.basic th.place') is not None


def parse_race_list_page(html_or_soup):
    """レース選択のページから、各レースのレース結果のURLを返す（重複は除く）"""
    soup = _soup(html_or_soup)
    urls = []
    for a in soup.find_all('a', href=True):
        m = re.search(r'CNAME=(pw01sde\d+/[0-9A-Fa-f]{2})', a['href'])
        if m:
            url = JRA_RESULT_URL + m.group(1)
            if url not in urls:
                urls.append(url)
    return urls


def parse_kaisai_page(html_or_soup, today=None):
    """レース結果の「開催選択」ページ（JRAから直接取得したもの）から、
    [{'label': '4回東京2日', 'date': date, 'cname': 'pw01srl…'}] を返す。
    MHTMLで保存したページはクリック時の動作（onclick）が消えているため、cnameは取れない"""
    soup = _soup(html_or_soup)
    today = today or date.today()
    result, seen = [], set()
    for a in soup.find_all('a'):
        label = _text(a)
        m = re.fullmatch(r'(\d+)回(\S+?)(\d+)日', label)
        if not m:
            continue
        onclick = a.get('onclick') or ''
        cm = re.search(r"doAction\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", onclick)
        head = a.find_previous(['h3', 'h2'])
        dm = re.search(r'(\d{1,2})月(\d{1,2})日', _text(head)) if head else None
        race_date = None
        if dm:
            month, day = int(dm.group(1)), int(dm.group(2))
            year = today.year - (1 if month > today.month + 1 else 0)
            race_date = date(year, month, day)
        key = (label, race_date)
        if key in seen:
            continue
        seen.add(key)
        result.append({'label': label, 'date': race_date,
                       'action': cm.group(1) if cm else '', 'cname': cm.group(2) if cm else ''})
    return result


def parse_race_result_page(html_or_soup, url=''):
    """レース結果のページから {'race': {...}, 'runners': [{...}, ...]} を返す"""
    soup = _soup(html_or_soup)
    header = soup.select_one('div.race_header')
    if header is None:
        raise ValueError('レース結果のページではありません。')

    race = {}
    cname = ''
    for a in soup.find_all('a', href=True):
        m = re.search(r'CNAME=(pw01sde\d+/[0-9A-Fa-f]{2})', a['href'])
        if m and header in a.parents:
            cname = m.group(1)
            break
    if not cname:
        m = re.search(r'CNAME=(pw01sde\d+/[0-9A-Fa-f]{2})', url or '')
        cname = m.group(1) if m else ''
    race['race_id'] = race_id_from_cname(cname)
    race['url'] = JRA_RESULT_URL + cname if cname else (url or '')

    date_text = _text(header.select_one('.date_line .cell.date'))
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', date_text)
    if not m:
        raise ValueError('レースの年月日を読み取れませんでした。')
    race['date'] = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    km = re.search(r'(\d+)回(\S+?)(\d+)日', date_text)
    race['kai'], race['venue'], race['nichi'] = (int(km.group(1)), km.group(2), int(km.group(3))) if km else ('', '', '')

    tm = re.search(r'(\d{1,2})時(\d{2})分', _text(header.select_one('.date_line .cell.time')))
    race['time'] = f"{int(tm.group(1))}:{tm.group(2)}" if tm else ''
    race['weather'] = _text(header.select_one('li.weather .txt'))
    race['turf'] = _text(header.select_one('li.turf .txt'))
    race['dirt'] = _text(header.select_one('li.durt .txt')) or _text(header.select_one('li.dirt .txt'))

    num_img = header.select_one('.race_number img')
    nm = re.search(r'(\d+)', (num_img.get('alt') if num_img else '') or '')
    race['num'] = int(nm.group(1)) if nm else (int(race['race_id'][-10:-8]) if len(race['race_id']) >= 10 else '')

    title = header.select_one('.race_title')
    race['name'] = _text(title.select_one('.race_name')) if title else ''
    grade = ''
    if title:
        for img in title.select('img'):
            grade = normalize_grade(img.get('alt'))
            if grade:
                break
    race['grade'] = grade

    def cell(cls):
        return _text(header.select_one(f'.type .cell.{cls}'))
    category = cell('category')
    race['jump'] = category.startswith('障害')
    race['category'] = category.replace('障害', '', 1) if race['jump'] else category  # 2歳／3歳／3歳以上
    race['class'] = cell('class')            # 新馬／未勝利／1勝クラス／…／オープン
    race['rule'] = cell('rule')              # （混合）［指定］など
    race['weight_rule'] = cell('weight')     # 馬齢／定量／別定／ハンデ
    course = header.select_one('.type .cell.course')
    course_text = _text(course)
    dm = re.search(r'([\d,]+)\s*メートル', course_text)
    race['distance'] = int(dm.group(1).replace(',', '')) if dm else ''
    detail = _text(course.select_one('.detail')) if course else ''
    race['course_detail'] = detail.strip('（）()')
    if race['jump']:
        race['surface'] = '障'
    elif 'ダート' in detail:
        race['surface'] = 'ダ'
    elif '芝' in detail:
        race['surface'] = '芝'
    else:
        race['surface'] = ''

    prizes = []
    for unit in header.select('ul.prize .prize_unit'):
        if '本賞金' in _text(unit.select_one('.cap')):
            for li in unit.select('ol li'):
                pm = re.search(r'([\d,]+)', _text(li.select_one('.num')))
                prizes.append(int(pm.group(1).replace(',', '')) if pm else 0)
            break
    race['prizes'] = prizes  # 1着〜5着の本賞金（万円）

    table = None
    for t in soup.select('table.basic'):
        if t.select_one('th.place'):
            table = t
            break
    runners = []
    for tr in (table.select('tbody tr') if table else []):
        def td(cls):
            return tr.select_one(f'td.{cls}')
        place = _text(td('place'))
        waku_img = td('waku').select_one('img') if td('waku') else None
        wm = re.search(r'(\d+)', (waku_img.get('alt') if waku_img else '') or _text(td('waku')))
        horse_a = td('horse').select_one('a') if td('horse') else None
        age_text = _text(td('age'))
        am = re.match(r'(牡|牝|せん|セン|騸)(\d+)', age_text)
        jockey_td = td('jockey')
        mark = _text(jockey_td.select_one('.mark')) if jockey_td else ''
        jockey = _text(jockey_td.select_one('a')) if jockey_td and jockey_td.select_one('a') else _text(jockey_td).replace(mark, '').strip()
        corners = [_text(li) for li in td('corner').select('li')] if td('corner') else []
        hw_td = td('h_weight')
        hw_diff = _text(hw_td.select_one('span')) if hw_td else ''
        hw_text = _text(hw_td).replace(hw_diff, '').strip() if hw_td else ''
        place_num = int(place) if place.isdigit() else None
        runner = {
            'place': place,
            'waku': int(wm.group(1)) if wm else '',
            'num': _text(td('num')),
            'name': _text(horse_a) if horse_a else _text(td('horse')),
            'horse_url': horse_a.get('href', '') if horse_a else '',
            'sex': ('せん' if am and am.group(1) in ('せん', 'セン', '騸') else am.group(1)) if am else '',
            'age': int(am.group(2)) if am else '',
            'weight': _text(td('weight')),
            'mark': mark,
            'jockey': jockey,
            'time': _text(td('time')),
            'margin': _text(td('margin')),
            'corners': '-'.join(c for c in corners if c),
            'last': _text(td('f_time')),
            'body_weight': hw_text,
            'body_diff': hw_diff.strip('()（）'),
            'trainer': _text(td('trainer')),
            'pop': _text(td('pop')),
            'prize': prizes[place_num - 1] if place_num and place_num <= len(prizes) else 0,
        }
        runner['horse_id'] = horse_id_from_url(runner['horse_url'])
        runners.append(runner)
    race['runners_count'] = len([r for r in runners if r['place'] not in ('取消', '除外')])
    return {'race': race, 'runners': runners}


# --- 収得賞金・クラス ---

def is_special_race_name(race_name):
    """競走名のある特別競走か（競走名が無いレースは「障害3歳以上未勝利」のような条件がレース名欄に入る）"""
    name = (race_name or '').strip()
    return bool(name) and not re.match(r'^(障害)?\d歳', name)

def _floor10(man):
    """10万円未満の端数を切り捨てる（単位は万円）"""
    return int(man) // 10 * 10


def _age_group(category):
    """年齢条件を 2歳／3歳／3以上 に分ける（「3歳以上」「4歳以上」は3以上）"""
    if '以上' in category:
        return '3以上'
    if category.startswith('2歳'):
        return '2歳'
    if category.startswith('3歳'):
        return '3歳'
    return '3以上'


def shutoku_amount(race_date, jump, category, race_class, grade, race_name, rule, place, prize):
    """1回の出走で収得賞金に算入する額（万円）。算入しない場合は0"""
    if place not in (1, 2):
        return 0
    graded = grade in ('G1', 'G2', 'G3')
    if place == 2 and not graded:
        return 0
    if race_date and race_date < NEW_SHUTOKU_RULE_FROM:
        # 従前の取扱い：新馬・未勝利の第1着は400万円、それ以外は該当する着順の本賞金の半額
        if place == 1 and not graded and race_class in ('新馬', '未勝利'):
            return 400
        return _floor10((prize or 0) / 2)

    age = _age_group(category)
    if graded:
        if age == '2歳' and grade == 'G3' and not jump:
            return 1600 if place == 1 else 600
        return _floor10((prize or 0) / 2)
    # ここからは第1着のみ
    if race_class == 'オープン' or grade == 'L':
        if jump:
            return 750 if is_special_race_name(race_name) else 600   # 特別競走（競走名あり）／一般競走
        listed = grade == 'L'
        if age == '2歳':
            if listed:
                return 800
            return 500 if '九州産' in (rule or '') + (race_name or '') else 600
        if age == '3歳':
            return 1200 if listed else 1000
        return 1400 if listed else 1200
    return {'3勝クラス': 900, '2勝クラス': 600, '1勝クラス': 500, '新馬': 400, '未勝利': 400}.get(race_class, 0)


def class_from_shutoku(amount, has_started, jump=False):
    """収得賞金（万円）からクラスを求める"""
    if jump:
        if amount <= 0:
            return '障害未勝利' if has_started else '障害未出走'
        return '障害オープン'
    if amount <= 0:
        return '未勝利' if has_started else '新馬'
    if amount <= 500:
        return '1勝クラス'
    if amount <= 1000:
        return '2勝クラス'
    if amount <= 1600:
        return '3勝クラス'
    return 'オープン'


def summarize_results(results):
    """出走成績（日付順でなくてよい）から、本賞金の合計・収得賞金・クラスを平地／障害別に求める。
    results の各要素：{'date': date, 'jump': bool, 'category', 'class', 'grade', 'name', 'rule', 'place', 'prize'}"""
    summary = {}
    for kind, jump in (('flat', False), ('jump', True)):
        rows = [r for r in results if bool(r.get('jump')) == jump]
        started = [r for r in rows if r.get('place') not in ('取消', '除外', '')]
        total_prize = sum(int(r.get('prize') or 0) for r in rows)
        shutoku = 0
        for r in rows:
            place = int(r['place']) if str(r.get('place', '')).isdigit() else None
            r['shutoku'] = shutoku_amount(r.get('date'), jump, r.get('category', ''), r.get('class', ''),
                                          r.get('grade', ''), r.get('name', ''), r.get('rule', ''),
                                          place, int(r.get('prize') or 0))
            shutoku += r['shutoku']
        summary[kind] = {'starts': len(started), 'total_prize': total_prize, 'shutoku': shutoku,
                         'class': class_from_shutoku(shutoku, bool(started), jump)}
    return summary

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
from datetime import date, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

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
    if isinstance(html_or_soup, (BeautifulSoup, Tag)):
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


def parse_horse_race_urls(html_or_soup):
    """JRAの競走馬情報のページの「出走レース」の表から、各レースのレース結果のURLを返す（新しい順）"""
    soup = _soup(html_or_soup)
    urls = []
    for table in soup.find_all('table'):
        caption = table.find('caption')
        if not caption or '出走レース' not in _text(caption):
            continue
        for url in parse_race_list_page(table):
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


def parse_race_header(header, race_id=''):
    """レース結果・出馬表のページの見出し（div.race_header）から、年月日・開催・レース番号・レース名・条件・コース・
    本賞金などを読み取る"""
    race = {}
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
    race['num'] = int(nm.group(1)) if nm else (int(race_id[-10:-8]) if len(race_id or '') >= 10 else '')

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
    return race


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

    race.update(parse_race_header(header, race.get('race_id', '')))
    prizes = race['prizes']

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
            'icons': [img.get('alt', '') for img in td('horse').select('span.horse_icon img')] if td('horse') else [],
            # JRAから取得したページのリンクは「/JRADB/accessU.html?…」のような相対URLなので、完全なURLにする
            'horse_url': urljoin(JRA_RESULT_URL, horse_a.get('href', '')) if horse_a and horse_a.get('href') else '',
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

def prize_sum(prizes):
    """本賞金（万円）の合計。地方競馬の本賞金は1万円未満の端数があるため小数1桁にし、整数ならintで返す"""
    total = round(sum(float(p or 0) for p in prizes), 1)
    return int(total) if total == int(total) else total


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


# JRA所属馬が地方の指定交流競走（重賞以外）で1着になったときの収得賞金（交流競走の地方のクラスに当たるJRAのクラス）
NAR_KORYU_SHUTOKU = {'オープン': 1000, '3勝クラス': 900, '2勝クラス': 600, '1勝クラス': 500, '未勝利': 400}


def nar_shutoku_amount(place, prize, grade, affiliation, jra_class='', race_class=''):
    """地方競馬のレースの収得賞金（万円。JRAの番組 一般事項の「本会以外の競走」の取扱い）。
    1着：本賞金（B）が1,200万円以上なら1/2、400万円以上なら400万円、10万円以上ならB、10万円未満なら10万円。
    JRA所属馬が指定交流競走（重賞以外）で1着なら、地方のクラスに当たるJRAのクラス（jra_class）の額。
    2着：地方の重賞か、地方所属の期間で本賞金（S）が100万円以上なら、Sが480万円以上なら1/2、160万円以上なら160万円、
    それ未満ならS。いずれも10万円未満は切り捨て"""
    prize = prize or 0
    # 地方の重賞：Jpn1〜3（G1〜G3として記録）か、格組が「重賞」のレース
    graded = grade in ('G1', 'G2', 'G3') or _nfkc(race_class) == '重賞'
    jra = affiliation == 'JRA'
    if place == 1:
        if jra and not graded and jra_class in NAR_KORYU_SHUTOKU:
            return NAR_KORYU_SHUTOKU[jra_class]
        if prize >= 1200:
            return _floor10(prize / 2)
        if prize >= 400:
            return 400
        return _floor10(prize) if prize >= 10 else 10
    if place == 2 and (graded or (not jra and prize >= 100)):
        if prize >= 480:
            return _floor10(prize / 2)
        return 160 if prize >= 160 else _floor10(prize)
    return 0


def shutoku_amount(race_date, jump, category, race_class, grade, race_name, rule, place, prize,
                   nar=False, affiliation='', jra_class=''):
    """1回の出走で収得賞金に算入する額（万円）。算入しない場合は0。
    地方競馬のレース（nar=True）は nar_shutoku_amount（2021年6月4日以前も同じ扱い）"""
    if place not in (1, 2):
        return 0
    if nar:
        return nar_shutoku_amount(place, prize, grade, affiliation, jra_class, race_class)
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


def young_class_until(year):
    """2歳・3歳馬のクラスが「1勝クラスの次はオープン」になる最終日（3歳の5月の最後の開催週が終わるまで）。
    5月の最後の土曜日とその翌日の日曜日の開催が5月最後の開催週で、その日曜日まで
    （例：6月1日が土曜日なら5月26日まで、5月31日が土曜日なら6月1日まで）"""
    may31 = date(year, 5, 31)
    last_saturday = may31 - timedelta(days=(may31.weekday() - 5) % 7)
    return last_saturday + timedelta(days=1)


def is_young_class_period(birth_year, today=None):
    """2歳の間と、3歳の5月まで（young_class_until）か"""
    if not birth_year:
        return False
    today = today or date.today()
    age = today.year - int(birth_year)
    return age <= 2 or (age == 3 and today <= young_class_until(today.year))


def class_from_shutoku(amount, has_started, jump=False, birth_year=None, today=None):
    """収得賞金（万円）からクラスを求める。birth_year を指定すると、2歳と3歳の5月までは
    1勝クラスの次をオープンにする（2勝クラス・3勝クラスは3歳の6月から）"""
    if jump:
        if amount <= 0:
            return '障害未勝利' if has_started else '障害未出走'
        return '障害オープン'
    if amount <= 0:
        return '未勝利' if has_started else '新馬'
    if amount <= 500:
        return '1勝クラス'
    if is_young_class_period(birth_year, today):
        return 'オープン'
    if amount <= 1000:
        return '2勝クラス'
    if amount <= 1600:
        return '3勝クラス'
    return 'オープン'


# 降級制度：2014年以前生まれの馬は、4歳の夏季競馬の開始時点で平地収得賞金を1/2にしていた（2019年夏季競馬から廃止。
# 2015年生まれ以降は対象外。1/2になった収得賞金は廃止後も元に戻らない）
KOKYU_LAST_BIRTH_YEAR = 2014


def summer_racing_start(year):
    """夏季競馬の開始日（日本ダービーの翌週、6月の最初の土曜日）"""
    d = date(year, 6, 1)
    return date(year, 6, 1 + (5 - d.weekday()) % 7)


_OLD_CLASS_NAMES = {'1600万下': '3勝クラス', '1000万下': '2勝クラス', '500万下': '1勝クラス'}


def race_level(r):
    """レースのクラス（オープン・3勝クラス・2勝クラス・1勝クラス・未勝利・新馬）。重賞・リステッドはオープン。
    地方のレースは重賞（Jpn）だけオープンとし、それ以外は空文字"""
    if r.get('grade') in ('G1', 'G2', 'G3', 'L'):
        return 'オープン'
    if r.get('nar'):
        return ''
    cls = str(r.get('class') or '').strip()
    cls = _OLD_CLASS_NAMES.get(cls, cls)
    return 'オープン' if cls in ('オープン', 'OP') else cls


def summarize_results(results, birth_year=None, today=None):
    """出走成績（日付順でなくてよい）から、本賞金の合計・収得賞金・クラスを平地／障害別に求める。
    results の各要素：{'date': date, 'jump': bool, 'category', 'class', 'grade', 'name', 'rule', 'place', 'prize'}
    birth_year を指定すると、2014年以前生まれの馬は4歳夏季の降級（平地収得賞金を1/2）を反映する"""
    today = today or date.today()
    kokyu_date = None
    if birth_year and int(birth_year) <= KOKYU_LAST_BIRTH_YEAR:
        kokyu_date = summer_racing_start(int(birth_year) + 4)
    summary = {}
    for kind, jump in (('flat', False), ('jump', True)):
        rows = sorted([r for r in results if bool(r.get('jump')) == jump],
                      key=lambda r: (r.get('date') or date.min, str(r.get('num', '')).zfill(2)))
        started = [r for r in rows if r.get('place') not in ('取消', '除外', '')]
        total_prize = prize_sum(r.get('prize') for r in rows)
        shutoku = 0
        kokyu = None
        for r in rows:
            if not jump and kokyu_date and not kokyu and r.get('date') and r['date'] >= kokyu_date:
                kokyu = {'date': kokyu_date, 'before': shutoku, 'after': shutoku // 2}
                shutoku //= 2
            place = int(r['place']) if str(r.get('place', '')).isdigit() else None
            r['shutoku'] = shutoku_amount(r.get('date'), jump, r.get('category', ''), r.get('class', ''),
                                          r.get('grade', ''), r.get('name', ''), r.get('rule', ''),
                                          place, r.get('prize') or 0, nar=bool(r.get('nar')),
                                          affiliation=r.get('affiliation', ''), jra_class=r.get('nar_jra_class', ''))
            shutoku += r['shutoku']
        if not jump and kokyu_date and not kokyu and today >= kokyu_date and shutoku:
            kokyu = {'date': kokyu_date, 'before': shutoku, 'after': shutoku // 2}
            shutoku //= 2
        def _record(runs):
            places = [str(r.get('place', '')) for r in runs]
            rec = [places.count('1'), places.count('2'), places.count('3')]
            rec.append(len(runs) - sum(rec))   # 4着以下（競走中止・失格なども含む）
            return rec
        current = class_from_shutoku(shutoku, bool(started), jump, birth_year, today)
        # 現クラスの成績：現在のクラスと同じクラスのレースをすべて通算する
        # （オープンなら2歳・3歳・3歳以上・4歳以上のオープン（重賞・リステッドを含む）をすべて含む）
        target = current.replace('障害', '')
        in_class = [r for r in rows if race_level(r) == target and r.get('place') not in ('取消', '除外', '')]
        summary[kind] = {'starts': len(started), 'record': _record(started), 'total_prize': total_prize,
                         'shutoku': shutoku, 'class': current, 'kokyu': kokyu,
                         'class_starts': len(in_class), 'class_record': _record(in_class)}
    return summary


# --- 開催日程（競馬番組）の日別ページ ---
# https://www.jra.go.jp/keiba/calendar2026/2026/11/1101.html（事前に発表された各日の番組）

def program_url(race_date):
    y, m, d = race_date.year, race_date.month, race_date.day
    return f"https://www.jra.go.jp/keiba/calendar{y}/{y}/{m}/{m:02d}{d:02d}.html"


_PROGRAM_GRADE_RE = re.compile(r'[（(]\s*((?:J・)?G[ⅠⅡⅢ123]|Jpn[ⅠⅡⅢ]|L)\s*[）)]\s*$')
_PROGRAM_CLASS_RE = re.compile(r'^(障害)?(\d歳(?:以上)?)?(.*)$')


def split_program_race_name(text):
    """「第174回 天皇賞（秋）（GⅠ）」を（レース名, グレード）＝（天皇賞（秋）, G1）に分ける"""
    text = re.sub(r'\s+', ' ', (text or '')).strip()
    grade = ''
    m = _PROGRAM_GRADE_RE.search(text)
    if m:
        grade = normalize_grade(m.group(1))
        text = text[:m.start()].strip()
    text = re.sub(r'^第\s*\d+\s*回\s*', '', text).strip()
    return text, grade


def parse_program_page(html_or_soup, page_date=None):
    """開催日程の日別ページから、その日の全レースの番組を返す（競馬場の表示順・レース番号順）"""
    soup = _soup(html_or_soup)
    if page_date is None:
        head = _text(soup.title) + ' ' + _text(soup.select_one('#contentsBody') or soup.body)
        m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日[（(].曜[）)]\s*競馬番組', head)
        if not m:
            raise ValueError('開催日程（競馬番組）のページではありません。')
        page_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    races = []
    for table in soup.select('#program_list table'):
        cm = re.search(r'(\d+)回(\S+?)(\d+)日', _text(table.find('caption')))
        if not cm:
            continue
        kai, venue, nichi = int(cm.group(1)), cm.group(2), int(cm.group(3))
        for tr in table.select('tbody tr'):
            nm = re.match(r'(\d+)', _text(tr.select_one('th.num')))
            if not nm:
                continue
            name_td = tr.select_one('td.name')
            name, grade = split_program_race_name(_text(name_td.select_one('p.stakes')) if name_td else '')
            condition = _text(name_td.select_one('p.race_class')) if name_td else ''
            cls = _PROGRAM_CLASS_RE.match(condition)
            jump = bool(cls.group(1))
            dist_text = _text(name_td.select_one('.race_cond .dist')) if name_td else ''
            dm = re.search(r'[\d,]+', dist_text)
            type_text = _text(name_td.select_one('.race_cond .type')) if name_td else ''
            parts = re.findall(r'[（(]([^）)]*)[）)]', type_text)
            course_detail = parts[0] if parts else ''
            marks = ''.join(f"（{p}）" for p in parts[1:])
            weight = re.sub(r'[（(][^）)]*[）)]', '', type_text).strip()
            tm = re.search(r'(\d{1,2})時(\d{2})分', _text(tr.select_one('td.time')))
            races.append({
                'date': page_date, 'venue': venue, 'kai': kai, 'nichi': nichi, 'num': int(nm.group(1)),
                'name': name, 'grade': grade, 'jump': jump,
                'category': cls.group(2) or '', 'class': (cls.group(3) or '').strip(), 'condition': condition,
                'distance': int(dm.group(0).replace(',', '')) if dm else '',
                'surface': '障' if jump else ('ダ' if course_detail.startswith('ダ') else ('芝' if course_detail.startswith('芝') else '')),
                'course_detail': course_detail, 'marks': marks, 'weight_rule': weight,
                'time': f"{int(tm.group(1))}:{tm.group(2)}" if tm else '',
            })
    return races


# --- 特別レース登録馬（競馬メニューの「特別レース登録馬」） ---
SPECIAL_LIST_CNAME = 'pw03trl00/29'
_SPECIAL_CNAME_RE = re.compile(r'(pw03tde\d{2}(\d{2})(\d{2})(\d{2})(\d{2})(\d{8})/[0-9A-Fa-f]{2})')
_CLASS_TEXT_RE = re.compile(r'^(障害)?\s*(\d歳(?:以上)?)?\s*(.*)$')


def split_class_text(text):
    """「3歳以上2勝クラス」「障害3歳以上オープン」を (障害か, 年齢条件, クラス) に分ける"""
    m = _CLASS_TEXT_RE.match(re.sub(r'\s+', '', text or ''))
    return bool(m.group(1)), m.group(2) or '', (m.group(3) or '').strip()


def _surface_of(text):
    if '障害' in text:
        return '障'
    if 'ダート' in text or 'ダ' in text:
        return 'ダ'
    return '芝' if '芝' in text else ''


def parse_special_list_page(html_or_soup):
    """特別レース登録馬の一覧のページから、各レースの {'cname', 'date', 'venue', 'kai', 'nichi', 'num', 'name',
    'grade', 'jump', 'category', 'class', 'distance', 'surface', 'marks', 'weight_rule'} を返す"""
    soup = _soup(html_or_soup)
    races = []
    for a in soup.find_all('a', onclick=True):
        m = _SPECIAL_CNAME_RE.search(a['onclick'])
        if not m:
            continue
        cname, venue_code, kai, nichi, num, ymd = m.groups()
        race_date = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
        name = ''.join(s for s in a.find_all(string=True, recursive=False)).strip()
        grade = ''
        for img in a.select('.grade_icon img'):
            grade = normalize_grade(img.get('alt')) or grade
        jump, category, race_class = split_class_text(_text(a.select_one('.cell.class')))
        dist = _text(a.select_one('.cell.dist'))
        dm = re.search(r'[\d,]+', dist)
        cap = _text(a.select_one('.cap'))
        wm = re.search(r'(馬齢|定量|別定|ハンデ)\s*$', cap)
        races.append({
            'cname': cname, 'date': race_date, 'venue': VENUES.get(venue_code, venue_code),
            'kai': int(kai), 'nichi': int(nichi), 'num': int(num), 'name': name, 'grade': grade,
            'jump': jump or '障害' in dist, 'category': category, 'class': race_class,
            'distance': int(dm.group(0).replace(',', '')) if dm else '',
            'surface': '障' if jump else _surface_of(dist),
            'marks': cap[:wm.start()].strip() if wm else cap, 'weight_rule': wm.group(1) if wm else '',
        })
    if not races and '特別レース登録馬' not in _text(soup.title) + _text(soup.body)[:3000]:
        raise ValueError('特別レース登録馬のページではありません。')
    return races


def parse_special_race_page(html_or_soup):
    """特別レース登録馬の各レースのページから、登録馬 [{'name', 'url', 'horse_id', 'weight', 'added'}] と、
    ページから読み取れたレースの情報（年月日・競馬場・回・日・レース番号・レース名・グレードなど。読み取れない項目は無し）を返す"""
    soup = _soup(html_or_soup)
    horses = []
    for td in soup.select('td.horse'):
        a = td.find('a')
        name = _text(a or td)
        if not name or name == '馬名':
            continue
        tr = td.find_parent('tr')
        url = a.get('href', '') if a else ''
        horses.append({'name': name, 'url': url, 'horse_id': horse_id_from_url(url),
                       'weight': _text(tr.select_one('td.weight')) if tr else '',
                       'added': '追加' in _text(tr.select_one('td.add')) if tr else False})
    race = {}
    text = _text(soup.select_one('div.race_header') or soup.body or soup)
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', text)
    if m:
        race['date'] = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    km = re.search(r'(\d+)回\s*(札幌|函館|福島|新潟|東京|中山|中京|京都|阪神|小倉)\s*(\d+)日', text)
    if km:
        race['kai'], race['venue'], race['nichi'] = int(km.group(1)), km.group(2), int(km.group(3))
    num_img = soup.select_one('.race_number img')
    nm = re.search(r'(\d+)', (num_img.get('alt') if num_img else '') or '') or re.search(r'(\d{1,2})\s*レース', text)
    if nm:
        race['num'] = int(nm.group(1))
    title = soup.select_one('.race_title .race_name') or soup.select_one('.race_name')
    if title:
        race['name'] = _text(title)
    for img in soup.select('.race_title img, .race_name img, .grade_icon img'):
        g = normalize_grade(img.get('alt'))
        if g:
            race['grade'] = g
            break
    # 年齢条件・クラス・記号・重量（ページの「3歳以上」「オープン」「（国際）（特指）」「ハンデ」）
    for key, sel in (('category', '.type .category'), ('class', '.type .class'), ('marks', '.type .rule'),
                     ('weight_rule', '.type .weight')):
        el = soup.select_one(sel)
        if el and _text(el):
            race[key] = _text(el)
    # 出走可能頭数（「出走可能頭数 18頭」「出走可能頭数：18頭」「フルゲート18頭」など）
    page_text = _text(soup.body or soup).replace('：', ':')
    cm = re.search(r'出走可能頭数\s*:?\s*(\d+)\s*頭', page_text) or re.search(r'フルゲート\s*:?\s*(\d+)\s*頭', page_text)
    if cm:
        race['capacity'] = int(cm.group(1))
    return {'race': race, 'horses': horses, 'capacity': race.get('capacity')}


def parse_horse_transfers(html_or_soup):
    """JRAの競走馬情報のページの「出走レース」の表から、リンクの無い「JRAから転出」「JRAへ転入」の行を
    [{'type': '転出'／'転入', 'date': 日付}] で返す"""
    soup = _soup(html_or_soup)
    out = []
    for table in soup.find_all('table'):
        caption = table.find('caption')
        if not caption or '出走レース' not in _text(caption):
            continue
        for tr in table.find_all('tr'):
            text = _text(tr)
            kind = '転出' if 'JRAから転出' in text else ('転入' if 'JRAへ転入' in text else '')
            m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', text)
            if kind and m:
                item = {'type': kind, 'date': date(int(m.group(1)), int(m.group(2)), int(m.group(3)))}
                if item not in out:
                    out.append(item)
    return out


# --- 地方競馬（地方競馬情報サイト keiba.go.jp） ---
NAR_BASE = 'https://www.keiba.go.jp/KeibaWeb/'
# JRAの出走レースの表で、リンクの無い行の競馬場がこれらなら地方競馬のレース（地方競馬情報サイトの所属場の地域）
NAR_VENUES = ('帯広', '門別', '札幌', '旭川', '岩見沢', '北見', '盛岡', '水沢', '上山', '新潟', '三条', '足利', '宇都宮',
              '高崎', '浦和', '船橋', '大井', '川崎', '金沢', '笠松', '名古屋', '中京', '園田', '姫路', '益田', '福山',
              '高知', '佐賀', '荒尾', '中津')
NAR_RACE_RESULT_URL = NAR_BASE + 'TodayRaceInfo/RaceMarkTable'
_NAR_RACE_URL_RE = re.compile(r'RaceMarkTable\?k_raceDate=(\d{4})(?:%2F|/)(\d{1,2})(?:%2F|/)(\d{1,2})&(?:amp;)?k_raceNo=(\d+)&(?:amp;)?k_babaCode=(\d+)')
# 地方の重賞の格付け「JpnＩ」「JpnII」「JpnⅢ」など。直後に「２歳」などが続いても（「JpnII２歳」）、
# 格付けの部分だけを取るように長いものから順に試す
_NAR_GRADE_RE = re.compile(r'(Jpn|JPN|ＪＰＮ|Ｊｐｎ)\s*(Ⅲ|Ⅱ|Ⅰ|III|II|I|ＩＩＩ|ＩＩ|Ｉ|lll|ll|l|[1-3１-３](?![0-9０-９]))')


def nar_text(el):
    """地方競馬情報サイトの文字（全角スペースも詰める）"""
    return re.sub(r'[\s　]+', ' ', el.get_text(' ') if el else '').strip()


def nar_person_name(text):
    """地方競馬情報サイトの人名（「山　崎　　裕　也」のように1文字ずつ全角スペースで区切り、姓と名の間は2つ）を
    「山崎 裕也」にする"""
    text = (text or '').strip()
    text = re.sub(r'[（(][^）)]*[）)]\s*$', '', text).strip()   # 「（JRA）」「（退厩）」などの所属を除く
    text = re.sub(r'　{2,}|\s{2,}', ' ', text)
    return re.sub(r'[　]', '', text).strip()


def nar_race_url(race_date, race_no, baba_code):
    return (f"{NAR_RACE_RESULT_URL}?k_raceDate={race_date.year}%2F{race_date.month:02d}%2F{race_date.day:02d}"
            f"&k_raceNo={int(race_no)}&k_babaCode={int(baba_code)}")


def parse_nar_race_url(url):
    """地方のレース結果のURLから (年月日, レース番号, 競馬場コード)。読み取れなければ None"""
    m = _NAR_RACE_URL_RE.search(url or '')
    if not m:
        return None
    y, mo, d, no, baba = m.groups()
    return date(int(y), int(mo), int(d)), int(no), int(baba)


def nar_race_id(race_date, race_no, baba_code):
    """地方のレースのレースID（N年月日・競馬場コード2桁・レース番号2桁）"""
    return f"N{race_date.strftime('%Y%m%d')}{int(baba_code):02d}{int(race_no):02d}"


def nar_grade(text):
    """「JpnＩ」「JpnⅡ」などをG1〜G3にそろえる（地方重賞の格付け。該当しなければ空文字）"""
    m = _NAR_GRADE_RE.search(text or '')
    if not m:
        return ''
    roman = m.group(2).translate(str.maketrans('ＩIlｌ１２３', 'ⅠⅠⅠⅠ123'))
    roman = {'ⅠⅠⅠ': '3', 'ⅠⅠ': '2', 'Ⅰ': '1', 'Ⅱ': '2', 'Ⅲ': '3'}.get(roman, roman)
    return f"G{roman}" if roman in ('1', '2', '3') else ''


def _yen_to_man(text):
    """円を万円にする（端数があれば小数）"""
    m = re.search(r'([\d,]+)', text or '')
    if not m:
        return 0
    yen = int(m.group(1).replace(',', ''))
    man = yen / 10000
    return int(man) if man == int(man) else round(man, 1)


def parse_nar_race_page(html_or_soup, url=''):
    """地方競馬のレース結果のページ（RaceMarkTable）から {'race': {...}, 'runners': [...], 'day_urls': [...]} を返す
    （JRAのレース結果と同じ項目。地方のみの項目：runner の affiliation（所属）・jockey_url・trainer_url）"""
    soup = _soup(html_or_soup)
    head = next((h for h in soup.find_all('h4') if '競走' in h.get_text()), None)
    title = soup.select_one('section.raceTitle')
    table = soup.select_one('section.gradeTable table')
    if head is None or title is None or table is None:
        raise ValueError('地方競馬のレース結果のページではありません。')
    head_text = nar_text(head).replace(' ', '')
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日.*?[）)](.+?)第(\d+)競走', head_text)
    if not m:
        raise ValueError('レースの年月日・競馬場を読み取れませんでした。')
    race_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    venue, num = m.group(4), int(m.group(5))
    info = parse_nar_race_url(url)
    if not info:
        # 保存したページ：同じ競馬場のレース番号のボタンのリンクから競馬場コードを読む
        for a in soup.select('div.raceNumNavi a[href]'):
            info = parse_nar_race_url(a['href'])
            if info:
                break
    baba = info[2] if info else 0
    name_text = nar_text(title.select_one('h3'))
    grade = nar_grade(name_text)
    # レース名：「第３９回」と、グレード・格組（「JpnＩオープン」「３歳２」など）を除く
    name = re.sub(r'^第\s*[\d０-９]+\s*回\s*', '', name_text)
    name = re.sub(r'\s*〔[^〕]*〕\s*', ' ', name).strip()   # 「〔Ｒｏａｄ ｔｏ ＪＢＣ〕」などの副題を除く
    # クラス（格組）はレース名からは判定しない（馬の出走履歴のページ HorseMarkInfo の格組の列を使う：
    # apply_nar_class）。グレード（JpnＩなど）から後ろはレース名に含めない
    race_class = ''
    gm = _NAR_GRADE_RE.search(name)
    if gm:
        # 格付けの部分だけを除く（「全日本２歳優駿【国際交流JpnＩ】」→「全日本２歳優駿【国際交流】」）。
        # 後ろに続く条件・格組（「兵庫ジュニアグランプリJpnII２歳」の「２歳」、「JpnＩオープン」の「オープン」）は除き、
        # 閉じかっこだけ残す
        rest = re.match(r'[\s】）)〕」]*', name[gm.end():]).group(0)
        name = (name[:gm.start()] + rest).strip()
    data = [nar_text(li) for li in title.select('ul.dataArea li')]
    first = data[0] if data else ''
    # 九州産馬限定（「たんぽぽ賞３歳九州産」）：レース名の末尾の「九州産」は除き、記号に「九州産」を入れる
    kyushu = '九州産' in first
    if _nfkc(name).endswith('九州産') and len(name) > 3:
        name, kyushu = name[:-3].strip(), True
    dm = re.search(r'(ダート|芝|障害)\s*([\d,]+)\s*[ｍm]', first)
    surface = {'ダート': 'ダ', '芝': '芝', '障害': '障'}.get(dm.group(1), '') if dm else ''
    wm = re.search(r'天候[:：]\s*(\S+)', first)
    gm2 = re.search(r'馬場[:：]\s*(\S+)', first)
    cm = re.search(r'(\d歳(?:以上)?)', first)
    rm = re.search(r'(馬齢|定量|別定|ハンデ)', first)
    prizes = []
    for pm in re.finditer(r'(\d)着\s*([\d,]+)円', ' '.join(data[1:])):
        prizes.append(_yen_to_man(pm.group(2)))
    race = {
        'nar': True, 'race_id': nar_race_id(race_date, num, baba), 'url': nar_race_url(race_date, num, baba),
        'baba_code': baba, 'date': race_date, 'kai': '', 'nichi': '', 'venue': venue, 'num': num,
        'name': name, 'grade': grade, 'jump': surface == '障',
        'category': cm.group(1) if cm else '', 'class': race_class,
        'rule': ' '.join(x for x in ('牝' if re.search(r'(^|\s)牝(馬)?(\s|$)', first) else '',
                                     '九州産' if kyushu else '') if x),
        'weight_rule': rm.group(1) if rm else '',
        'distance': int(dm.group(2).replace(',', '')) if dm else '', 'surface': surface,
        'course_detail': '', 'time': '', 'weather': wm.group(1) if wm else '',
        'turf': gm2.group(1) if gm2 and surface == '芝' else '', 'dirt': gm2.group(1) if gm2 and surface != '芝' else '',
        'prizes': prizes,
    }
    runners = []
    for tr in table.find_all('tr'):
        tds = {(td.get('class') or [''])[0]: td for td in tr.find_all('td')}
        if 'd' not in tds:
            continue
        place = nar_text(tds.get('a'))
        horse_a = tds['d'].find('a')
        sex_age = nar_text(tds.get('f')).replace(' ', '')
        am = re.match(r'(牡|牝|せん|セン|騸)(\d+)', sex_age)
        jockey_td, trainer_td = tds.get('h'), tds.get('i')
        jockey_a = jockey_td.find('a') if jockey_td else None
        trainer_a = trainer_td.find('a') if trainer_td else None
        jspan = jockey_td.find('span') if jockey_td else None
        jockey_aff = nar_text(jspan).strip('（）() ') if jspan else ''
        jockey_short = nar_text(jockey_td).replace(nar_text(jspan), '').strip() if jockey_td else ''
        hw = tds.get('j')
        hw_span = hw.find('span') if hw else None
        place_num = int(place) if place.isdigit() else None
        runner = {
            'place': place, 'waku': nar_text(tds.get('b')), 'num': nar_text(tds.get('c')),
            'name': nar_text(horse_a or tds['d']), 'icons': [],
            'horse_url': urljoin(NAR_BASE, horse_a.get('href', '')) if horse_a else '',
            'affiliation': nar_text(tds.get('e')),
            'sex': ('せん' if am and am.group(1) in ('せん', 'セン', '騸') else am.group(1)) if am else '',
            'age': int(am.group(2)) if am else '',
            'weight': nar_text(tds.get('g')), 'mark': '',
            'jockey': jockey_short, 'jockey_affiliation': jockey_aff,
            'jockey_url': urljoin(NAR_BASE, jockey_a.get('href', '')) if jockey_a else '',
            'time': nar_text(tds.get('k')), 'margin': nar_text(tds.get('l')), 'last': nar_text(tds.get('m')),
            'corners': nar_text(tds.get('n')).replace(' ', ''),
            'body_weight': nar_text(hw).replace(nar_text(hw_span), '').strip() if hw else '',
            'body_diff': nar_text(hw_span).strip('()（） ') if hw_span else '',
            'trainer': nar_text(trainer_a or trainer_td),
            'trainer_url': urljoin(NAR_BASE, trainer_a.get('href', '')) if trainer_a else '',
            'pop': nar_text(tds.get('o')),
            'prize': prizes[place_num - 1] if place_num and place_num <= len(prizes) else 0,
        }
        lm = re.search(r'k_lineageLoginCode=(\d+)', runner['horse_url'])
        runner['horse_id'] = ''          # JRAの馬IDではないため空にする（馬名で照合する）
        runner['nar_id'] = lm.group(1) if lm else ''
        runners.append(runner)
    race['runners_count'] = len([r for r in runners if r['place'] not in ('取消', '除外', '')])
    day_urls = []
    for a in soup.select('div.raceNumNavi a'):
        href = urljoin(NAR_BASE, a.get('href', '')) if a.get('href') else race['url']
        if parse_nar_race_url(href) and href not in day_urls:
            day_urls.append(href)
    if race['url'] not in day_urls:
        day_urls.append(race['url'])
    return {'race': race, 'runners': runners, 'day_urls': day_urls}


def parse_nar_horse_mark_page(html_or_soup):
    """地方競馬の馬出走履歴のページ（HorseMarkInfo）から、出走したレースの一覧と地方競馬登録抹消日を返す。
    {'name', 'races': [{'date', 'venue', 'num', 'name', 'url'}], 'cancel_date'}"""
    soup = _soup(html_or_soup)
    table = soup.select_one('table.HorseMarkInfo_table')
    races = []
    for tr in (table.select('tbody tr') if table else []):
        tds = tr.find_all('td')
        if len(tds) < 4:
            continue
        dm = re.match(r'(\d{4})/(\d{1,2})/(\d{1,2})', nar_text(tds[0]))
        a = tds[3].find('a')
        url = a.get('href', '') if a else ''
        if not dm or not parse_nar_race_url(url):
            continue
        info = parse_nar_race_url(url)
        races.append({'date': date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3))), 'venue': nar_text(tds[1]),
                      'num': nar_text(tds[2]), 'name': nar_text(tds[3]), 'url': urljoin(NAR_BASE, url),
                      'race_id': nar_race_id(*info), 'class': nar_text(tds[4]) if len(tds) > 4 else ''})
    text = nar_text(soup.body or soup)
    cm = re.search(r'(\d{4})/(\d{1,2})/(\d{1,2})\s*地方競馬登録抹消', text)
    name_el = soup.find('h4', class_='odd_title') or soup.find('h3')
    return {'name': nar_text(name_el), 'races': races,
            'cancel_date': date(int(cm.group(1)), int(cm.group(2)), int(cm.group(3))) if cm else None}


def _nfkc(text):
    import unicodedata
    return unicodedata.normalize('NFKC', str(text or '')).replace(' ', '')


def nar_name_has_class(name, race_class):
    """レース名にクラス（格組）の表記が含まれているか（全角・半角は区別しない）"""
    return bool(_nfkc(race_class)) and _nfkc(race_class) in _nfkc(name)


def apply_nar_class(race, race_class):
    """地方のレースのクラスに格組を入れ、レース名の末尾に格組が付いていれば除く
    （「頬白（ほおじろ）賞３歳２」→「頬白（ほおじろ）賞」。レース名が格組だけなら、そのまま残す）"""
    race_class = (race_class or '').strip()
    race['class'] = race_class
    name, n = race.get('name', ''), len(race_class)
    if n and len(name) > n and _nfkc(name[-n:]) == _nfkc(race_class):
        race['name'] = name[:-n].strip()
    return race


def parse_nar_horse_list_page(html_or_soup):
    """地方競馬の競走馬検索の結果の一覧（RaceHorseList）：[{'name', 'url', 'birth_year', 'affiliation', 'sire', 'dam'}]"""
    soup = _soup(html_or_soup)
    out = []
    for table in soup.select('table.RaceHorseList'):
        for tr in table.find_all('tr'):
            tds = tr.find_all('td')
            if len(tds) < 7 or not tds[1].find('a'):
                continue
            by = nar_text(tds[3])
            out.append({'name': nar_text(tds[1]), 'url': urljoin(NAR_BASE, tds[1].find('a').get('href', '')),
                        'birth_year': int(by) if by.isdigit() else None, 'affiliation': nar_text(tds[4]),
                        'sire': nar_text(tds[5]), 'dam': nar_text(tds[6])})
    return out


def parse_nar_person_page(html_or_soup):
    """地方競馬の騎手・調教師のページ（RiderMark／TrainerMark）：{'name'（「山崎 裕也」）, 'affiliation'}"""
    soup = _soup(html_or_soup)
    name_el = soup.find('h4', class_='odd_title')
    aff = ''
    for td in soup.find_all('td'):
        if td.get_text(strip=True) == '所属':
            nxt = td.find_next_sibling('td')
            aff = nar_text(nxt)
            break
    return {'name': nar_person_name(name_el.get_text('') if name_el else ''), 'affiliation': aff}


def parse_horse_nar_rows(html_or_soup):
    """JRAの競走馬情報のページの「出走レース」の表で、リンクの無い地方競馬のレースの行
    [{'date', 'venue', 'name'}]（競馬場が地方競馬の競馬場の行だけ）"""
    soup = _soup(html_or_soup)
    out = []
    for table in soup.find_all('table'):
        caption = table.find('caption')
        if not caption or '出走レース' not in _text(caption):
            continue
        for tr in table.find_all('tr'):
            if tr.find('a', href=re.compile(r'pw01sde')):
                continue
            cells = [_text(td) for td in tr.find_all('td')]
            if len(cells) < 3:
                continue
            m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', cells[0])
            venue = cells[1].replace(' ', '')
            if m and venue in NAR_VENUES:
                out.append({'date': date(int(m.group(1)), int(m.group(2)), int(m.group(3))), 'venue': venue,
                            'name': cells[2]})
    return out


# --- 出馬投票の出走決定順（JRAの「競馬番組等について」の出走馬決定方法と、JRAのよくある質問） ---
# 各年度の変更（競馬番組等について）：
#   2021年度：3(4)歳以上2勝クラス 2勝クラス馬のうち4節以内の前走5着以内馬（それまでは3着以内）
#   2022年度：3(4)歳以上3勝クラス 3勝クラス馬のうち4節以内の前走3着以内馬、以下クラス順に出走間隔の長い馬
#   2023年度：平地の3(4)歳以上オープン（重賞・リステッドを除く） オープン馬のうち8節以内の前走3着以内馬、以下同様
#   2025年度：3(4)歳以上3勝クラス 前走成績の対象を8節以内に拡大
#   2026年度：2・3歳1勝クラス（それまでは次走優先権）、障害のオープン（一般競走） オープン馬のうち8節以内の前走3着以内馬
# 未勝利・1勝クラスは自ブロック所属馬（美浦所属馬は中山・東京、栗東所属馬は京都・阪神の開催。第3場は対象外）を優先する。
# 2勝クラスは移籍後初戦の馬・未出走馬の条件がある（JRAのよくある質問）
# ※前走成績は中央競馬の平地競走（障害は障害競走）の成績に限る。
# ※ハンデキャップ競走（3勝クラスは2022年度、オープンは2023年度から）はハンデ上位3頭を最優先とする
_CLASS_ORDER = ['オープン', '3勝クラス', '2勝クラス', '1勝クラス', '未勝利']
BLOCK_VENUES = {'中山': '美浦', '東京': '美浦', '京都': '栗東', '阪神': '栗東'}   # 競馬場 → 自ブロックの所属


def entry_order_rule(race_date, jump, category, race_class, grade='', race_name='', weight_rule='', venue=''):
    """出走決定順の決め方。[{'kind': 'handicap'／'form'／'transfer'／'unraced'／'interval', 'class', 'within'（節）,
    'place'（着以内）, 'block'（'own'／'other'／None）, 'label'}]。当てはまるルールが無ければ None"""
    if not race_date:
        return None
    year = race_date.year
    cls = (race_class or '').strip()
    young = (category or '').strip() in ('2歳', '3歳')
    blocks = venue in BLOCK_VENUES   # 自ブロック・他ブロックで分ける開催か（第3場は分けない）

    def step(kind, label, c=None, block=None, **kw):
        return dict(kind=kind, label=label, block=block, **{'class': c}, **kw)

    def form(c, within, place):
        return step('form', f"{c}馬のうち、{within}節以内の前走{place}着以内馬", c, within=within, place=place)

    def interval(c):
        return step('interval', f"{c}馬のうち、出走間隔の長い馬", c)

    def by_block(make):
        # 自ブロック → 他ブロックの順（第3場は分けずに1回）
        return make('own') + make('other') if blocks else make(None)

    handicap = {'kind': 'handicap', 'label': 'ハンデ上位3頭', 'block': None, 'class': None}
    if jump:
        if cls == 'オープン' and not grade and not is_special_race_name(race_name) and year >= 2026:
            return [form('オープン', 8, 3), interval('オープン'), interval('未勝利')]
        return None
    who = lambda b: {'own': '自ブロック所属馬', 'other': '他ブロック所属馬'}.get(b, '馬')
    if cls == '未勝利':
        return by_block(lambda b: [
            step('form', f"4節以内の前走5着以内の{who(b)}", '未勝利', b, within=4, place=5),
            step('unraced', f"未出走馬のうち{who(b)}" if b else '未出走馬', None, b),
            step('interval', f"出走間隔の長い{who(b)}", '未勝利', b)])
    if cls == '1勝クラス' and (not young or year >= 2026):
        return by_block(lambda b: [
            step('form', f"4節以内の前走5着以内の{who(b)}（未勝利馬を除く）", '1勝クラス', b, within=4, place=5),
            step('transfer', f"地方や外国での出走経験があり、JRAに移籍後初戦の{who(b)}", None, b),
            step('interval', f"出走間隔の長い{who(b)}（未勝利馬を除く）", '1勝クラス', b)]) + by_block(lambda b: [
            step('unraced', f"未出走馬のうち{who(b)}" if b else '未出走馬', None, b),
            step('interval', f"未勝利馬のうち出走間隔の長い{who(b)}", '未勝利', b)])
    if cls == '2勝クラス' and not young:
        return [form('2勝クラス', 4, 5 if year >= 2021 else 3),
                step('transfer', '地方や外国での出走経験があり、JRAに移籍後初戦の2勝クラス馬', '2勝クラス'),
                interval('2勝クラス'),
                step('transfer', '地方や外国での出走経験があり、JRAに移籍後初戦の1勝クラス馬', '1勝クラス'),
                interval('1勝クラス'), step('unraced', '未出走馬'), interval('未勝利')]
    if cls == '3勝クラス' and not young and year >= 2022:
        steps = [form('3勝クラス', 8 if year >= 2025 else 4, 3)] + [interval(c) for c in _CLASS_ORDER[1:]]
        return ([handicap] if weight_rule == 'ハンデ' else []) + steps
    if cls == 'オープン' and not young and not grade and year >= 2023:
        steps = [form('オープン', 8, 3)] + [interval(c) for c in _CLASS_ORDER]
        return ([handicap] if weight_rule == 'ハンデ' else []) + steps
    return None


def race_week_index(d):
    """節（開催の週）の番号：その週の木曜日を基準にした週の通し番号"""
    thursday = d - timedelta(days=(d.weekday() - 3) % 7)
    return thursday.toordinal() // 7


def entry_horse_class(results, race_date, jump, birth_year):
    """出走決定順の判定に使う、その日の馬のクラス（オープン・3勝クラス・2勝クラス・1勝クラス・未勝利）"""
    s = summarize_results([r for r in results if r.get('date') and r['date'] < race_date],
                          birth_year=birth_year, today=race_date)['jump' if jump else 'flat']
    cls = s['class'].replace('障害', '')
    return '未勝利' if cls in ('新馬', '未出走', '未勝利') else cls


def entry_order(horses, rule, race_date, jump=False, venue=''):
    """出走決定順を付ける。horses：[{'key', 'results'（出走成績）, 'birth_year', 'weight', 'area'（美浦・栗東）}]。
    {key: (順位, 条件の番号)} を返す（どの条件にも当てはまらない馬は含めない）。同じ順位は同じ番号"""
    week = race_week_index(race_date)
    own_area = BLOCK_VENUES.get(venue)
    info = {}
    for h in horses:
        past = sorted([r for r in h.get('results') or [] if r.get('date') and r['date'] < race_date],
                      key=lambda r: r['date'], reverse=True)
        jra_same = [r for r in past if not r.get('nar') and bool(r.get('jump')) == bool(jump)]
        prev = jra_same[0] if jra_same else None
        area = (h.get('area') or '').strip()
        info[h['key']] = {
            'class': entry_horse_class(past, race_date, jump, h.get('birth_year')),
            # 前走（中央の平地競走。障害は障害競走）が何節前か、着順
            'prev_weeks': week - race_week_index(prev['date']) if prev else None,
            'prev_place': int(prev['place']) if prev and str(prev.get('place', '')).isdigit() else None,
            'unraced': not past,
            # JRAに移籍後初戦：地方（または外国）で出走経験があり、最後の出走が地方のレース
            'transfer': bool(past) and bool(past[0].get('nar')),
            # 出走間隔（節）。初出走馬は最も長い
            'interval': week - race_week_index(past[0]['date']) if past else 10 ** 6,
            'weight': float(h['weight']) if re.fullmatch(r'\d+(\.\d+)?', str(h.get('weight') or '')) else None,
            # 自ブロック所属馬か（所属が分からない馬は自ブロックとして扱う）
            'own': not own_area or not area or area == own_area,
        }
    done, out, position = set(), {}, 1
    for gi, step in enumerate(rule):
        block = step.get('block')
        cands = {k: v for k, v in info.items() if k not in done
                 and (block is None or (block == 'own') == v['own'])
                 and (not step.get('class') or v['class'] == step['class'])}
        if step['kind'] == 'handicap':
            weights = sorted([v['weight'] for v in info.values() if v['weight'] is not None], reverse=True)
            if len(weights) < 3:
                continue
            group = [(k, 0) for k, v in cands.items() if v['weight'] is not None and v['weight'] >= weights[2]]
        elif step['kind'] == 'form':
            group = [(k, 0) for k, v in cands.items() if v['prev_weeks'] is not None and v['prev_weeks'] <= step['within']
                     and v['prev_place'] is not None and v['prev_place'] <= step['place']]
        elif step['kind'] == 'transfer':
            group = [(k, 0) for k, v in cands.items() if v['transfer']]
        elif step['kind'] == 'unraced':
            group = [(k, 0) for k, v in cands.items() if v['unraced']]
        else:
            group = [(k, -v['interval']) for k, v in cands.items()]
        group.sort(key=lambda x: x[1])
        prev_key, prev_pos = None, position
        for i, (k, sort_key) in enumerate(group):
            pos = prev_pos if sort_key == prev_key else position + i
            out[k] = (pos, gi)
            prev_key, prev_pos = sort_key, pos
            done.add(k)
        position += len(group)
    return out


# --- 出馬表（JRAの競馬メニューの「出馬表」） ---
JRA_ENTRY_URL = "https://www.jra.go.jp/JRADB/accessD.html"
_DENMA_CNAME_RE = re.compile(r'(pw01dde\d\d(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})(\d{8})/[0-9A-Fa-f]{2})')


def denma_cnames(html_or_text):
    """ページの中の出馬表のレースへのリンク（CNAME）を {開催日のキー: {レース番号: CNAME}} で返す。
    開催日のキー：場2桁・年4桁・回2桁・日2桁・年月日8桁（レース結果の day_key と同じ形）"""
    text = str(html_or_text)
    out = {}
    for m in _DENMA_CNAME_RE.finditer(text):
        cname, venue, year, kai, nichi, num, ymd = m.groups()
        out.setdefault(f"{venue}{year}{kai}{nichi}{ymd}", {}).setdefault(int(num), cname)
    return out


def denma_day_label(key):
    """開催日のキーから「2026年10月24日 4回東京8日」"""
    venue, kai, nichi, ymd = key[:2], int(key[6:8]), int(key[8:10]), key[10:18]
    return f"{int(ymd[:4])}年{int(ymd[4:6])}月{int(ymd[6:8])}日 {kai}回{VENUES.get(venue, venue)}{nichi}日"


def parse_denma_page(html_or_soup):
    """出馬表のページから {'race': {...}, 'horses': [{'name', 'url', 'horse_id', 'waku', 'num', 'sex', 'age',
    'weight', 'jockey', 'trainer'}], 'day_races': {レース番号: CNAME}} を返す（馬番が無ければ枠順確定前）"""
    soup = _soup(html_or_soup)
    header = soup.select_one('div.race_header')
    if header is None:
        raise ValueError('出馬表のページではありません。')
    race = parse_race_header(header)
    if not race.get('date') or not race.get('venue'):
        raise ValueError('レースの年月日・競馬場を読み取れませんでした。')
    horses = []
    for tr in soup.select('table.basic tbody tr'):
        name_td = tr.select_one('td.horse')
        if not name_td:
            continue
        a = name_td.select_one('.name a') or name_td.find('a')
        name = _text(a or name_td.select_one('.name'))
        if not name:
            continue
        url = a.get('href', '') if a else ''
        waku_img = tr.select_one('td.waku img')
        wm = re.search(r'(\d)', (waku_img.get('alt') if waku_img else '') or _text(tr.select_one('td.waku')))
        num = _text(tr.select_one('td.num'))
        age_text = _text(tr.select_one('td.jockey .age') or tr.select_one('.age'))
        am = re.match(r'(牡|牝|せん|セン|騸)\s*(\d+)', age_text)
        weight = re.search(r'([\d.]+)', _text(tr.select_one('td.jockey .weight') or tr.select_one('p.weight')))
        trainer = name_td.select_one('p.trainer a') or name_td.select_one('p.trainer')
        horses.append({'name': name, 'url': url, 'horse_id': horse_id_from_url(url),
                       'waku': wm.group(1) if wm else '', 'num': num if num.isdigit() else '',
                       'sex': ('せん' if am.group(1) in ('せん', 'セン', '騸') else am.group(1)) if am else '',
                       'age': int(am.group(2)) if am else '',
                       'weight': weight.group(1) if weight else '',
                       'jockey': _text(tr.select_one('td.jockey p.jockey')),
                       'trainer': _text(trainer)})
    day_key = f"{next((c for c, v in VENUES.items() if v == race['venue']), '')}{race['date'].year}" \
              f"{int(race['kai'] or 0):02d}{int(race['nichi'] or 0):02d}{race['date'].strftime('%Y%m%d')}"
    return {'race': race, 'horses': horses, 'day_key': day_key,
            'day_races': denma_cnames(str(soup)).get(day_key, {})}

from takuro_collector.sites.kinoshita import KinoshitaAdapter


def test_kin_actual_shape_cleanup():
    html = '''
    <html><head><title>リベール坂戸 201号室 | 木下の賃貸</title></head><body>
      <h1>リベール坂戸 201号室</h1>
      <dl>
        <dt>所在地</dt><dd>神奈川県川崎市高津区坂戸1-2-3</dd>
        <dt>家賃</dt><dd>88,000円</dd>
        <dt>共益費</dt><dd>2,000円</dd>
        <dt>敷金</dt><dd>無料</dd>
        <dt>礼金</dt><dd>賃料1カ月分</dd>
        <dt>専有面積</dt><dd>45.36㎡</dd>
        <dt>間取り</dt><dd>2DK</dd>
        <dt>築年月</dt><dd>1994/09</dd>
        <dt>階層</dt><dd>2階/地上2階</dd>
        <dt>構造</dt><dd>木造</dd>
        <dt>方位</dt><dd>南 鍵交換費 27,500円（税込）</dd>
        <dt>入居可能日</dt><dd>2026/09/12 物件ID 11941*5</dd>
        <dt>交通</dt><dd>JR南武線 武蔵新城駅 徒歩14分</dd>
      </dl>
    </body></html>
    '''
    p = KinoshitaAdapter().parse(html, 'https://kinoshita-chintai.com/details/13553_4details.html')
    assert p.building_name == 'リベール坂戸'
    assert p.room == '201'
    assert p.address == '神奈川県川崎市高津区坂戸1-2-3'
    assert p.rent == 88000
    assert p.management_fee == 2000
    assert p.orientation == '南'
    assert p.move_in_date == '2026/09/12'
    assert p.floor == '2階'
    assert p.total_floors == '2階'
    assert p.transport and p.transport[0].get('station') == '武蔵新城'
    assert p.transport[0].get('walk_minutes') == 14

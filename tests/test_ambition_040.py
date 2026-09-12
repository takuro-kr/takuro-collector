from takuro_collector.sites.ambition import AmbitionAdapter


def test_ambition_extracts_identity_and_primary_price_plan():
    html = """
    <html>
      <head><title>ヴェール・ラヴニール201（11.6m²-1R-7万円）【34291】</title></head>
      <body>
        <h1>更新 【公式】ヴェール・ラヴニール [アパート] ヴェールラヴニール</h1>
        <h3 class="rooms_title">【公式】ヴェール・ラヴニール 201 【アパート】ヴェールラヴニール</h3>
        <table>
          <tr><th>所在地</th><td>東京都板橋区東新町2丁目13-16</td></tr>
          <tr><th>交通</th><td>東武鉄道東上線/ときわ台 徒歩10分</td></tr>
        </table>
        <table>
          <tr><th>プラン名</th><th>賃料</th><th>敷金／礼金</th><th>管理費・共益費</th></tr>
          <tr><td>オールゼロプラン</td><td>65,000円</td><td>なし／なし</td><td>4,000円</td></tr>
          <tr><td>スーパーゼロプラン</td><td>75,000円</td><td>なし／なし</td><td>4,000円</td></tr>
        </table>
      </body>
    </html>
    """

    item = AmbitionAdapter().parse(html, "https://pm.am-bition.jp/rent/2591/34291")

    assert item.source_property_id == "34291"
    assert item.management_company == "アンビション"
    assert item.building_name == "ヴェール・ラヴニール"
    assert item.room == "201"
    assert item.prefecture == "東京都"
    assert item.rent == 65000
    assert item.management_fee == 4000
    assert item.deposit == "0"
    assert item.key_money == "0"

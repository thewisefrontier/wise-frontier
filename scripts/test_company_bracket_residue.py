# 실사례 327975 (스페이스엑스 기업 분석): "주당 83[83 달러]로"
from company_analysis_writer import strip_bracket_residue as f
assert f("주당 83[83 달러]로 상장, 111.50[111.5 달러]") == "주당 83 달러로 상장, 111.5 달러"
assert f("15000억[1조5000억 달러] 미만") == "1조5000억 달러 미만"
assert f("[사업] 목록 [1] 참조") == "[사업] 목록 [1] 참조"
print("ok")

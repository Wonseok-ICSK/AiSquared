export const LABELS = Object.freeze({
  sightseeing: '관광', food: '맛집', relaxation: '휴식', resort: '호텔·리조트', shopping: '쇼핑', nightlife: '나이트라이프', nature: '자연', photography: '사진', culture: '문화 체험', local: '현지 체험',
  total: '총 여행 예산', flight: '항공권', hotel: '숙박', transport: '교통', activities: '액티비티', other: '기타',
  ECONOMY: '일반석', PREMIUM_ECONOMY: '프리미엄 일반석', BUSINESS: '비즈니스석', FIRST: '일등석',
});
const WORDS = Object.freeze({
  Seoul: '서울', Tokyo: '도쿄', Osaka: '오사카', Fukuoka: '후쿠오카', Bangkok: '방콕', Singapore: '싱가포르', Paris: '파리', London: '런던', 'New York': '뉴욕',
  Incheon: '인천', Gimpo: '김포', Narita: '나리타', Haneda: '하네다', Kansai: '간사이', Itami: '이타미', Suvarnabhumi: '수완나품', Changi: '창이', 'Charles de Gaulle': '샤를 드골', Heathrow: '히스로', 'John F. Kennedy': '존 F. 케네디',
  'Jeju Air': '제주항공', 'Korean Air': '대한항공', 'Asiana Airlines': '아시아나항공', 'T’way Air': '트리니티항공',
  'Air Busan': '에어부산', 'Air Premia': '에어프레미아', 'Air Seoul': '에어서울', 'Peach Aviation': '피치항공', 'ZIPAIR Tokyo': '집에어', 'Trinity Airways': '트리니티항공', 'Jin Air': '진에어', 'EASTAR JET': '이스타항공', 'Google Flights · live': 'Google 항공편 · 실제 요금',
  'A long weekend in Tokyo': '도쿄에서 보내는 긴 주말', 'An Osaka food escape': '오사카 맛집 여행',
  Sushi: '스시', Ramen: '라멘', Yakitori: '야키토리', Soba: '소바', Izakaya: '이자카야', 'Local food': '현지 음식', Korean: '한식', Barbecue: '바비큐', Okonomiyaki: '오코노미야키',
  'Demo · sample fares': '데모 · 예시 항공권', 'Amadeus · live': 'Amadeus · 실시간', 'Amadeus · test': 'Amadeus · 테스트', 'Demo data': '데모 데이터', 'Amadeus test data': 'Amadeus 테스트 데이터',
  'Demo (simulated prices)': '데모 · 예시 요금', 'Amadeus (live / test)': 'Amadeus · 실시간 / 테스트',
  'Sample standard fare': '예시 일반 운임', '15 kg checked · sample': '위탁 수하물 15kg · 예시', '23 kg checked · sample': '위탁 수하물 23kg · 예시', 'Not provided': '정보 없음', 'Fare rules unavailable': '운임 규정 정보 없음',
  'Sample destination guide': '예시 여행 가이드', 'Your destination': '여행지', 'Nearby stop': '가까운 장소',
  'Asakusa & Ueno': '아사쿠사·우에노', 'Shibuya & Harajuku': '시부야·하라주쿠', Shinjuku: '신주쿠', 'Castle & central Osaka': '오사카성·도심', 'Namba & Dōtonbori': '난바·도톤보리', Jongno: '종로', Hongdae: '홍대',
  'Sensō-ji & Asakusa streets': '센소지와 아사쿠사 거리', 'Ueno Park': '우에노 공원', 'Ameyoko market': '아메요코 시장', 'Asakusa soba lunch': '아사쿠사 소바 점심', 'Ueno izakaya dinner': '우에노 이자카야 저녁',
  'Meiji Jingū & its forest': '메이지 신궁과 숲길', 'Harajuku backstreets': '하라주쿠 골목 산책', 'Shibuya crossing & neighborhood': '시부야 스크램블 교차로와 주변 거리', 'Shibuya ramen lunch': '시부야 라멘 점심', 'Shibuya sushi dinner': '시부야 스시 저녁',
  'Shinjuku Gyoen garden': '신주쿠 교엔', 'Tokyo Metropolitan Government observation decks': '도쿄도청 전망대', 'Shinjuku neighborhood walk': '신주쿠 거리 산책', 'Shinjuku yakitori dinner': '신주쿠 야키토리 저녁', 'Shinjuku ramen lunch': '신주쿠 라멘 점심',
  'Osaka Castle park': '오사카성 공원', 'Nakanoshima riverside': '나카노시마 강변', 'Central Osaka ramen lunch': '오사카 도심 라멘 점심', 'Dōtonbori food streets': '도톤보리 맛집 거리', 'Kuromon market': '구로몬 시장', 'Kuromon sushi lunch': '구로몬 스시 점심', 'Namba okonomiyaki dinner': '난바 오코노미야키 저녁',
  'Gyeongbokgung neighborhood': '경복궁 주변', 'Bukchon neighborhood walk': '북촌 골목 산책', 'Insadong tea & craft streets': '인사동 찻집·공예 거리', 'Jongno Korean lunch': '종로 한식 점심', 'Hongdae neighborhood': '홍대 거리', 'Hongdae barbecue dinner': '홍대 바비큐 저녁',
  'Transfer & settle in': '숙소 이동과 체크인', 'Flexible meal break': '자유 식사 시간', 'Free time': '자유 시간', 'Head to the airport': '공항으로 이동', 'Check-in & departure': '탑승 수속과 출발',
  'Arrive & find your rhythm': '도착 후 여유롭게 시작하기', 'One more wander': '마지막 날도 가볍게 둘러보기', 'A little closer to the city': '도시를 더 가까이 즐기기',
  'Built around your preferences, with nearby places grouped together and space to enjoy the journey.': '여행 취향에 맞춰 가까운 장소를 묶고, 여유롭게 즐길 시간을 마련했습니다.',
  'Allow for immigration, baggage, and finding transport. Late arrivals may continue after midnight.': '입국 심사, 수하물 수령, 교통편 확인 시간을 확보했습니다. 늦은 도착은 다음 날로 이어질 수 있습니다.',
  'Airport transfer and hotel check-in allowance. Adjust for your actual hotel location.': '공항에서 숙소까지 이동하고 체크인할 시간입니다. 실제 숙소 위치에 맞춰 조정해 주세요.',
  'Choose a nearby meal and check its cost and opening hours. Meal cost is not included in the estimate.': '근처 식당의 가격과 영업시간을 확인하고 선택해 주세요. 이 식사 비용은 예상 총액에 포함되지 않았습니다.',
  'Rest, explore nearby, or adjust around opening hours. Extra spending is not included.': '휴식을 취하거나 주변을 둘러보세요. 영업시간에 맞춰 조정할 수 있으며 추가 지출은 포함되지 않았습니다.',
  'Estimated transfer allowance. Leave three hours before departure, earlier for a longer route.': '예상 공항 이동 시간입니다. 출발 3시간 전에는 숙소를 나서고, 이동 거리가 길면 더 일찍 출발해 주세요.',
  'Sample guide: opening hours, closures, availability, ratings, and costs have not been verified. Restaurant entries are neighborhood search suggestions.': '예시 가이드의 영업시간, 휴무, 이용 가능 여부, 평점, 비용은 검증되지 않았습니다. 식당 항목은 주변 맛집 검색 제안입니다.',
  'Transit times and KRW costs are planning estimates. Check source links before traveling.': '이동 시간과 원화 비용은 계획용 예상치입니다. 여행 전 안내 링크에서 확인해 주세요.',
  'Google Places hours are regular weekly hours; verify holiday changes and reservations. Future availability and weather are not confirmed.': 'Google Places의 영업시간은 일반적인 주간 일정입니다. 공휴일 변경과 예약 가능 여부를 확인해 주세요. 미래 날씨와 이용 가능 여부는 확인되지 않았습니다.',
  'Transit times, admission costs, meal costs, and lodging allowances are KRW estimates; no live exchange rate is used.': '이동 시간, 입장료, 식비, 숙박 예산은 원화 예상치이며 실시간 환율을 적용하지 않았습니다.',
  'One-way flight: this plan assumes a four-day stay. Add a return flight to define the actual departure time.': '편도 항공편이므로 4일 체류를 가정했습니다. 실제 귀국 시간이 정해지면 왕복 여행으로 수정해 주세요.',
  'Airport timezone is not in the local catalog. Confirm all local times before traveling.': '해당 공항의 시간대 정보가 없습니다. 여행 전 현지 시간을 확인해 주세요.',
  'AI ranking was unavailable. This plan uses preference-based ranking and the same place data.': 'AI 추천을 사용할 수 없어 같은 장소 데이터를 바탕으로 여행 취향에 맞춰 구성했습니다.',
  'The chosen flight exceeds your flight budget.': '선택한 항공권이 항공 예산을 초과합니다.',
  'Flight and reserved allowances already exceed your total budget. Reduce those allowances or choose a cheaper flight.': '항공권과 확보한 예산만으로도 총예산을 초과합니다. 예산 항목을 조정하거나 더 저렴한 항공편을 선택해 주세요.',
  'Some suggestions were omitted to respect your spending limits.': '예산을 지키기 위해 일부 제안을 제외했습니다.',
  'Some meal breaks need your own nearby choice; their cost is not included in the total.': '일부 식사 시간에는 근처 식당을 직접 선택해야 합니다. 해당 비용은 예상 총액에 포함되지 않았습니다.',
  'Sample fare; no live availability is confirmed.': '예시 요금이며 실제 좌석이나 가격을 확인한 결과가 아닙니다.',
  'Provider fare rechecked. Availability and price can change before purchase.': '제공업체에서 요금을 다시 확인했습니다. 예약 전 가격과 좌석은 바뀔 수 있습니다.',
  'Search time limit reached; some variants were not checked.': '검색 시간 제한으로 일부 날짜·공항 조합을 확인하지 못했습니다.',
});
export function ko(value) {
  if (typeof value !== 'string') return value;
  if (Object.hasOwn(WORDS, value)) return WORDS[value];
  if (value.endsWith(', at your pace')) return `${ko(value.slice(0, -14))}, 나의 속도로`;
  return value.replace(/^Arrive at (.+)$/, '$1 공항 도착')
    .replace(/^Your flight leaves (.+) at (\d\d:\d\d)\. Confirm airline check-in deadlines\.$/, '$1 공항에서 $2에 출발합니다. 항공사의 탑승 수속 마감 시간을 확인해 주세요.')
    .replace(/^Meal suggestion · (.+)\. (.+)$/, (_, area, hours) => `식사 제안 · ${ko(area)}. ${hours}`)
    .replace(/^Explore · (.+)\. (.+)$/, (_, area, hours) => `둘러보기 · ${ko(area)}. ${hours}`)
    .replace('Fits regular published hours; verify exceptions.', '일반 영업시간에 맞췄습니다. 예외 일정은 확인해 주세요.')
    .replace('Opening hours unverified.', '영업시간 미확인.')
    .replace('Search time limit reached; some variants were not checked.', '검색 시간 제한으로 일부 날짜·공항 조합을 확인하지 못했습니다.')
    .replace(/Checked up to (\d+) of (\d+) date\/airport combinations\. Increase MAX_SEARCH_VARIANTS on the server for wider coverage\./, '날짜·공항 조합 $2개 중 최대 $1개를 확인했습니다. 검색 범위를 넓히려면 서버의 검색 한도를 늘려 주세요.')
    .replace(/(\d+) search variant\(s\) failed; results have partial coverage\./, '검색 조합 $1개가 실패했습니다. 일부 결과만 표시됩니다.')
    .replace(/(\d+) (?:KG|kg) checked$/, '위탁 수하물 $1kg')
    .replace(/(\d+) checked bag\(s\)/, '위탁 수하물 $1개');
}
export function foodKey(value) {
  const aliases = { 스시: 'sushi', 초밥: 'sushi', 라멘: 'ramen', 야키토리: 'yakitori', 소바: 'soba', 이자카야: 'izakaya', 오코노미야키: 'okonomiyaki', 한식: 'korean', 바비큐: 'barbecue' };
  return Object.hasOwn(aliases, value) ? aliases[value] : value.toLowerCase();
}
export function localizePlan(plan) {
  if (!plan) return plan;
  return { ...plan, title: ko(plan.title), explanation: ko(plan.explanation), source: ko(plan.source), warnings: plan.warnings.map(ko), days: plan.days.map(day => ({ ...day, area: ko(day.area), title: ko(day.title), events: day.events.map(event => ({ ...event, title: ko(event.title), description: ko(event.description), source: ko(event.source) })) })) };
}
export function koreanError(data) {
  if (data.issues) return data.issues.map(issue => `${LABELS[issue.path[0]] || ({ name: '여행 이름', origin: '출발지', destination: '도착지', departureDate: '출발일', returnDate: '귀국일', targetPrice: '목표 요금', maxPrice: '최대 요금', passengers: '인원', schedule: '확인 일정', budget: '예산', preferences: '여행 취향', alternativeDestinations: '대체 공항' })[issue.path[0]] || '입력값'}: 입력 내용을 확인해 주세요.`).join(' ');
  const message = data.error || '';
  const messages = {
    'Google Flights temporarily blocked searches. Try later or open Google Flights directly.': 'Google 항공편에서 자동 조회를 잠시 제한했습니다. 시간이 지난 뒤 다시 검색하거나 Google 항공편을 직접 열어 주세요.',
    'Google Flights page format changed. Open the search link to check prices.': 'Google 항공편 화면 형식이 바뀌어 요금을 읽을 수 없습니다. 검색 링크에서 직접 확인해 주세요.',
    'Microsoft Edge is unavailable. Install Edge to search real flight prices.': '실제 요금 검색에 사용할 Microsoft Edge를 찾을 수 없습니다. Edge를 설치해 주세요.',
    'Real-price search requires a live provider. Demo and test fares are not real prices.': '실제 요금 검색에는 실시간 서비스를 선택해 주세요. 데모·테스트 요금은 실제 가격이 아닙니다.',
    'The selected live flight provider is unavailable.': '선택한 실시간 항공권 검색 서비스를 사용할 수 없습니다. 연결 상태를 확인해 주세요.',
    'Choose a departure date today or later.': '출발일을 오늘 이후로 설정해 주세요.',
    'No matching return flight is available. Search prices again before generating a plan.': '일치하는 귀국편을 확인할 수 없습니다. 요금을 다시 검색한 뒤 여행 일정을 만들어 주세요.',
    'This flight is no longer priced. Run a new search.': '이 항공편의 요금을 확인할 수 없습니다. 항공권을 다시 검색해 주세요.',
    'Trip not found.': '여행을 찾을 수 없습니다.', 'Configure Amadeus server credentials before selecting this provider.': 'Amadeus를 선택하려면 먼저 서버에서 API 인증 정보를 설정해 주세요.',
    'A price check is already running for this trip.': '이 여행의 가격을 확인 중입니다. 잠시 기다려 주세요.', 'An itinerary is already being generated.': '여행 일정을 만들고 있습니다. 잠시 기다려 주세요.',
    'Selected flight is no longer in the latest results. Choose a current flight.': '선택한 항공편이 최신 결과에 없습니다. 항공편을 다시 선택해 주세요.', 'Choose a flight from the latest results.': '최신 검색 결과에서 항공편을 선택해 주세요.',
    'Trip changed during planning. Generate a fresh itinerary.': '일정을 만드는 동안 여행 설정이 바뀌었습니다. 다시 생성해 주세요.', 'Trip changed while searching. Run a fresh check.': '검색 중 여행 설정이 바뀌었습니다. 가격을 다시 확인해 주세요.',
    'Check fares and choose a flight before planning.': '가격을 확인하고 항공편을 선택한 뒤 일정을 만들어 주세요.',
    'This stay is too short for arrival formalities and return airport check-in. Choose a longer stay.': '입국 절차와 귀국 수속을 진행하기에 체류 시간이 너무 짧습니다. 더 긴 일정을 선택해 주세요.',
    'The MVP planner supports stays up to 21 days.': '현재 일정 생성은 최대 21일 체류까지 지원합니다.',
  };
  if (messages[message]) return messages[message];
  if (message.includes('sample guide covers')) return '예시 여행 가이드는 도쿄·오사카·서울을 지원합니다. 다른 여행지는 Google Places를 연결해 주세요.';
  if (message.includes('Amadeus')) return 'Amadeus 검색을 완료하지 못했습니다. 인증 정보, 조회 날짜, API 이용 한도를 확인해 주세요.';
  if (message.includes('Google Places')) return '장소 정보를 불러오지 못했습니다. Google Places 연결 상태를 확인해 주세요.';
  return '요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.';
}

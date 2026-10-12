import { useState } from 'react';
import { ExternalLink, LoaderCircle, Search, Bell, ShieldCheck } from 'lucide-react';
import { api, dateTime, money } from './api.js';
import { defaultTrip } from './TripForm.jsx';
import { FlightList } from './App.jsx';
import { LABELS, ko } from '../shared/ko.js';
import { googleFlightSearchUrl } from '../shared/flight-search.js';
import AmountInput from './AmountInput.jsx';

export default function FlightSearch({ meta, seed, onMonitor }) {
  const [form, setForm] = useState(() => ({ ...defaultTrip(), ...seed, provider: 'google_flights', maxPrice: 0, flexDeparture: 0, flexReturn: 0, nearbyAirports: false, alternativeDestinations: [] }));
  const [result, setResult] = useState(null), [busy, setBusy] = useState(''), [error, setError] = useState('');
  const set = (key, value) => { setForm(f => ({ ...f, [key]: value })); setResult(null); setError(''); };
  const providers = meta.providers.filter(p => p.live && p.configured);
  async function search(event) {
    event.preventDefault(); setBusy('search'); setError(''); setResult(null);
    try {
      const request = { ...form, name: form.name || `${form.origin} → ${form.destination} 실시간 여행`, returnDate: form.tripType === 'one_way' ? null : form.returnDate };
      setResult(await api('/flights/search', { method: 'POST', body: request }));
    } catch (e) { setError(e.message); } finally { setBusy(''); }
  }
  async function monitor() {
    setBusy('monitor'); setError('');
    try { await onMonitor(result.search); } catch (e) { setError(e.message); } finally { setBusy(''); }
  }
  return <section className="live-search">
    <div className="live-search-intro"><ShieldCheck size={21}/><div><b>지금 표시된 실제 항공권 요금을 검색하세요.</b><p>Google 항공편의 최신 표시 요금을 원화로 조회합니다. API 키 없이 사용할 수 있으며, 예약 시 최종 가격과 수하물을 확인하세요.</p></div></div>
    <form onSubmit={search} className="live-search-form">
      <fieldset disabled={!!busy}>
        <div className="field-grid">
          <label className="field"><span>출발지 · 공항 코드</span><input required list="live-airports" value={form.origin} maxLength={3} pattern="[A-Z]{3}" onChange={e => set('origin', e.target.value.toUpperCase())}/></label>
          <label className="field"><span>도착지 · 공항 코드</span><input required list="live-airports" value={form.destination} maxLength={3} pattern="[A-Z]{3}" onChange={e => set('destination', e.target.value.toUpperCase())}/></label>
          <label className="field"><span>여행 종류</span><select value={form.tripType} onChange={e => set('tripType', e.target.value)}><option value="round_trip">왕복</option><option value="one_way">편도</option></select></label>
          <label className="field"><span>여행 인원</span><select value={form.passengers} onChange={e => set('passengers', Number(e.target.value))}><option value={1}>성인 1명</option><option value={2}>성인 2명</option></select></label>
          <label className="field"><span>출발일</span><input type="date" required value={form.departureDate} onChange={e => set('departureDate', e.target.value)}/></label>
          <label className="field"><span>귀국일</span><input type="date" required={form.tripType === 'round_trip'} disabled={form.tripType === 'one_way'} min={form.departureDate} value={form.returnDate || ''} onChange={e => set('returnDate', e.target.value)}/></label>
          <label className="field"><span>좌석 등급</span><select value={form.cabin} onChange={e => set('cabin', e.target.value)}>{Object.entries(LABELS).filter(([k]) => ['ECONOMY', 'PREMIUM_ECONOMY', 'BUSINESS', 'FIRST'].includes(k)).map(([k, label]) => <option key={k} value={k}>{label}</option>)}</select></label>
          <label className="field"><span>실시간 검색 서비스</span><select value={form.provider} onChange={e => set('provider', e.target.value)}>{providers.map(p => <option key={p.id} value={p.id}>{ko(p.name)}</option>)}</select></label>
          <label className="field"><span>요금 기준</span><select value={form.priceBasis} onChange={e => set('priceBasis', e.target.value)}><option value="per_person">1인 기준 · 원</option><option value="total">전체 인원 기준 · 원</option></select></label>
          <label className="field"><span>최대 요금 · 원</span><AmountInput value={form.maxPrice} onValueChange={value => set('maxPrice', value)}/><small>0 또는 −1은 한도 없음입니다.</small></label>
          <label className="field"><span>모니터링 목표 요금 · 원</span><AmountInput required min={1} value={form.targetPrice} onValueChange={value => set('targetPrice', value)}/></label>
        </div>
        <datalist id="live-airports">{meta.airports.map(a => <option key={a.code} value={a.code}>{ko(a.city)} · {ko(a.name)}</option>)}</datalist>
        <div className="live-search-actions"><label className="check-row"><input type="checkbox" checked={form.directOnly} onChange={e => set('directOnly', e.target.checked)}/>직항만 검색</label><button type="submit" className="button primary" disabled={!providers.length}>{busy === 'search' ? <LoaderCircle size={17} className="spin"/> : <Search size={17}/>} {busy === 'search' ? '실제 요금 조회 중…' : '실제 항공권 검색'}</button></div>
      </fieldset>
    </form>
    {error && <div className="error-banner" role="alert">{error}<a className="text-button" href={googleFlightSearchUrl({ ...form, returnDate: form.tripType === 'one_way' ? null : form.returnDate })} target="_blank" rel="noreferrer">Google 항공편 직접 열기 <ExternalLink size={13}/></a></div>}
    {result && <div className="detail-panel live-results">
      <div className="live-result-heading"><div><span className="pill green">실제 표시 요금</span><h2>{result.search.origin} → {result.search.destination}</h2><p>{result.source} · 조회 {dateTime(result.offers[0]?.observedAt || result.checkedAt)} · {result.search.priceBasis === 'per_person' ? '1인 기준' : '전체 인원 기준'}{result.offers.length > 0 && ` · 최저 ${money(result.search.priceBasis === 'per_person' ? result.offers[0].pricePerPerson : result.offers[0].priceTotal)}`}</p></div><button className="button secondary" onClick={monitor} disabled={!!busy || !result.offers.length}>{busy === 'monitor' ? <LoaderCircle className="spin" size={17}/> : <Bell size={17}/>} 이 검색으로 모니터링</button></div>
      {result.warning && <div className="soft-note detail-warning">{ko(result.warning)}</div>}
      {result.search.tripType === 'round_trip' && <div className="soft-note detail-warning">왕복 요금은 표시된 출발편과 조합할 수 있는 귀국편 중 최저가부터입니다. 귀국편을 선택하면 최종 요금이 달라질 수 있습니다.</div>}
      <FlightList trip={{ ...result.search, offers: result.offers }} busy={busy}/>
    </div>}
  </section>;
}

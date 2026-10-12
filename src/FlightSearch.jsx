import { translate } from "./i18n.js";import { useState } from 'react';
import { ExternalLink, LoaderCircle, Search, Bell, ShieldCheck } from 'lucide-react';
import { api, dateTime, money } from './api.js';
import { defaultTrip } from './TripForm.jsx';
import { FlightList } from './App.jsx';
import { LABELS, ko } from '../shared/ko.js';
import { googleFlightSearchUrl } from '../shared/flight-search.js';
import AmountInput from './AmountInput.jsx';

export default function FlightSearch({ meta, seed, onMonitor }) {
  const [form, setForm] = useState(() => ({ ...defaultTrip(), ...seed, provider: 'google_flights', maxPrice: 0, flexDeparture: 0, flexReturn: 0, nearbyAirports: false, alternativeDestinations: [] }));
  const [result, setResult] = useState(null),[busy, setBusy] = useState(''),[error, setError] = useState('');
  const set = (key, value) => {setForm((f) => ({ ...f, [key]: value }));setResult(null);setError('');};
  const providers = meta.providers.filter((p) => p.live && p.configured);
  async function search(event) {
    event.preventDefault();setBusy('search');setError('');setResult(null);
    try {
      const request = { ...form, name: form.name || `${form.origin} → ${form.destination} 실시간 여행`, returnDate: form.tripType === 'one_way' ? null : form.returnDate };
      setResult(await api('/flights/search', { method: 'POST', body: request }));
    } catch (e) {setError(e.message);} finally {setBusy('');}
  }
  async function monitor() {
    setBusy('monitor');setError('');
    try {await onMonitor(result.search);} catch (e) {setError(e.message);} finally {setBusy('');}
  }
  return <section className="live-search">
    <div className="live-search-intro"><ShieldCheck size={21} /><div><b>{translate("\uC9C0\uAE08 \uD45C\uC2DC\uB41C \uC2E4\uC81C \uD56D\uACF5\uAD8C \uC694\uAE08\uC744 \uAC80\uC0C9\uD558\uC138\uC694.")}</b><p>{translate("Google \uD56D\uACF5\uD3B8\uC758 \uCD5C\uC2E0 \uD45C\uC2DC \uC694\uAE08\uC744 \uC6D0\uD654\uB85C \uC870\uD68C\uD569\uB2C8\uB2E4. API \uD0A4 \uC5C6\uC774 \uC0AC\uC6A9\uD560 \uC218 \uC788\uC73C\uBA70, \uC608\uC57D \uC2DC \uCD5C\uC885 \uAC00\uACA9\uACFC \uC218\uD558\uBB3C\uC744 \uD655\uC778\uD558\uC138\uC694.")}</p></div></div>
    <form onSubmit={search} className="live-search-form">
      <fieldset disabled={!!busy}>
        <div className="field-grid">
          <label className="field"><span>{translate("\uCD9C\uBC1C\uC9C0 \xB7 \uACF5\uD56D \uCF54\uB4DC")}</span><input required list="live-airports" value={form.origin} maxLength={3} pattern="[A-Z]{3}" onChange={(e) => set('origin', e.target.value.toUpperCase())} /></label>
          <label className="field"><span>{translate("\uB3C4\uCC29\uC9C0 \xB7 \uACF5\uD56D \uCF54\uB4DC")}</span><input required list="live-airports" value={form.destination} maxLength={3} pattern="[A-Z]{3}" onChange={(e) => set('destination', e.target.value.toUpperCase())} /></label>
          <label className="field"><span>{translate("\uC5EC\uD589 \uC885\uB958")}</span><select value={form.tripType} onChange={(e) => set('tripType', e.target.value)}><option value="round_trip">{translate("\uC655\uBCF5")}</option><option value="one_way">{translate("\uD3B8\uB3C4")}</option></select></label>
          <label className="field"><span>{translate("\uC5EC\uD589 \uC778\uC6D0")}</span><select value={form.passengers} onChange={(e) => set('passengers', Number(e.target.value))}><option value={1}>{translate("\uC131\uC778 1\uBA85")}</option><option value={2}>{translate("\uC131\uC778 2\uBA85")}</option></select></label>
          <label className="field"><span>{translate("\uCD9C\uBC1C\uC77C")}</span><input type="date" required value={form.departureDate} onChange={(e) => set('departureDate', e.target.value)} /></label>
          <label className="field"><span>{translate("\uADC0\uAD6D\uC77C")}</span><input type="date" required={form.tripType === 'round_trip'} disabled={form.tripType === 'one_way'} min={form.departureDate} value={form.returnDate || ''} onChange={(e) => set('returnDate', e.target.value)} /></label>
          <label className="field"><span>{translate("\uC88C\uC11D \uB4F1\uAE09")}</span><select value={form.cabin} onChange={(e) => set('cabin', e.target.value)}>{translate(Object.entries(LABELS).filter(([k]) => ['ECONOMY', 'PREMIUM_ECONOMY', 'BUSINESS', 'FIRST'].includes(k)).map(([k, label]) => <option key={k} value={k}>{translate(label)}</option>))}</select></label>
          <label className="field"><span>{translate("\uC2E4\uC2DC\uAC04 \uAC80\uC0C9 \uC11C\uBE44\uC2A4")}</span><select value={form.provider} onChange={(e) => set('provider', e.target.value)}>{translate(providers.map((p) => <option key={p.id} value={p.id}>{translate(ko(p.name))}</option>))}</select></label>
          <label className="field"><span>{translate("\uC694\uAE08 \uAE30\uC900")}</span><select value={form.priceBasis} onChange={(e) => set('priceBasis', e.target.value)}><option value="per_person">{translate("1\uC778 \uAE30\uC900 \xB7 \uC6D0")}</option><option value="total">{translate("\uC804\uCCB4 \uC778\uC6D0 \uAE30\uC900 \xB7 \uC6D0")}</option></select></label>
          <label className="field"><span>{translate("\uCD5C\uB300 \uC694\uAE08 \xB7 \uC6D0")}</span><AmountInput value={form.maxPrice} onValueChange={(value) => set('maxPrice', value)} /><small>{translate("0 \uB610\uB294 \u22121\uC740 \uD55C\uB3C4 \uC5C6\uC74C\uC785\uB2C8\uB2E4.")}</small></label>
          <label className="field"><span>{translate("\uBAA8\uB2C8\uD130\uB9C1 \uBAA9\uD45C \uC694\uAE08 \xB7 \uC6D0")}</span><AmountInput required min={1} value={form.targetPrice} onValueChange={(value) => set('targetPrice', value)} /></label>
        </div>
        <datalist id="live-airports">{translate(meta.airports.map((a) => <option key={a.code} value={a.code}>{translate(ko(a.city))} · {translate(ko(a.name))}</option>))}</datalist>
        <div className="live-search-actions"><label className="check-row"><input type="checkbox" checked={form.directOnly} onChange={(e) => set('directOnly', e.target.checked)} />{translate("\uC9C1\uD56D\uB9CC \uAC80\uC0C9")}</label><button type="submit" className="button primary" disabled={!providers.length}>{translate(busy === 'search' ? <LoaderCircle size={17} className="spin" /> : <Search size={17} />)} {translate(busy === 'search' ? '실제 요금 조회 중…' : '실제 항공권 검색')}</button></div>
      </fieldset>
    </form>
    {translate(error && <div className="error-banner" role="alert">{translate(error)}<a className="text-button" href={googleFlightSearchUrl({ ...form, returnDate: form.tripType === 'one_way' ? null : form.returnDate })} target="_blank" rel="noreferrer">{translate("Google \uD56D\uACF5\uD3B8 \uC9C1\uC811 \uC5F4\uAE30 ")}<ExternalLink size={13} /></a></div>)}
    {translate(result && <div className="detail-panel live-results">
      <div className="live-result-heading"><div><span className="pill green">{translate("\uC2E4\uC81C \uD45C\uC2DC \uC694\uAE08")}</span><h2>{translate(result.search.origin)} → {translate(result.search.destination)}</h2><p>{translate(result.source)}{translate(" \xB7 \uC870\uD68C ")}{translate(dateTime(result.offers[0]?.observedAt || result.checkedAt))} · {translate(result.search.priceBasis === 'per_person' ? '1인 기준' : '전체 인원 기준')}{translate(result.offers.length > 0 && ` · 최저 ${money(result.search.priceBasis === 'per_person' ? result.offers[0].pricePerPerson : result.offers[0].priceTotal)}`)}</p></div><button className="button secondary" onClick={monitor} disabled={!!busy || !result.offers.length}>{translate(busy === 'monitor' ? <LoaderCircle className="spin" size={17} /> : <Bell size={17} />)}{translate(" \uC774 \uAC80\uC0C9\uC73C\uB85C \uBAA8\uB2C8\uD130\uB9C1")}</button></div>
      {translate(result.warning && <div className="soft-note detail-warning">{translate(ko(result.warning))}</div>)}
      {translate(result.search.tripType === 'round_trip' && <div className="soft-note detail-warning">{translate("\uC655\uBCF5 \uC694\uAE08\uC740 \uD45C\uC2DC\uB41C \uCD9C\uBC1C\uD3B8\uACFC \uC870\uD569\uD560 \uC218 \uC788\uB294 \uADC0\uAD6D\uD3B8 \uC911 \uCD5C\uC800\uAC00\uBD80\uD130\uC785\uB2C8\uB2E4. \uADC0\uAD6D\uD3B8\uC744 \uC120\uD0DD\uD558\uBA74 \uCD5C\uC885 \uC694\uAE08\uC774 \uB2EC\uB77C\uC9C8 \uC218 \uC788\uC2B5\uB2C8\uB2E4.")}</div>)}
      <FlightList trip={{ ...result.search, offers: result.offers }} busy={busy} />
    </div>)}
  </section>;
}

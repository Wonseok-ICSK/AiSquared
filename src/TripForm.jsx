import { translate } from "./i18n.js";import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Check, ChevronLeft, Plane, SlidersHorizontal, Sparkles, X } from 'lucide-react';
import { api } from './api.js';
import AmountInput from './AmountInput.jsx';
import { ko, LABELS } from '../shared/ko.js';

const styles = ['sightseeing', 'food', 'relaxation', 'resort', 'shopping', 'nightlife', 'nature', 'photography', 'culture', 'local'];
export function defaultTrip() {
  const d = new Date();d.setDate(20);d.setMonth(d.getMonth() + 1);
  const departureDate = d.toLocaleDateString('en-CA');d.setDate(23);
  return { name: '', origin: 'ICN', destination: 'NRT', departureDate, returnDate: d.toLocaleDateString('en-CA'), tripType: 'round_trip', passengers: 2, cabin: 'ECONOMY', directOnly: true, departureWindow: { start: '00:00', end: '23:59' }, arrivalWindow: { start: '00:00', end: '23:59' }, flexDeparture: 0, flexReturn: 0, nearbyAirports: false, alternativeDestinations: [], provider: 'google_flights', targetPrice: 300000, maxPrice: 450000, priceBasis: 'per_person', schedule: { mode: 'daily', times: ['08:30', '17:30'], timezone: 'Asia/Seoul', intervalHours: 6 }, alertRules: { target: true, drop: false, dropPercent: 10, lowest: true }, preferences: { companion: 'couple', weights: Object.fromEntries(styles.map((s) => [s, s === 'food' || s === 'sightseeing' ? 40 : s === 'shopping' ? 20 : 0])), foods: ['스시', '라멘', '야키토리'], cuisine: '현지 음식', mealBudget: 30000, notes: '' }, budget: { total: 1500000, flight: 350000, hotel: 300000, food: 180000, transport: 70000, shopping: 100000, activities: 80000, other: 30000 } };
}
const num = (e) => Number(e.target.value);
function Field({ label, children, hint, className = '' }) {return <label className={`field ${className}`}><span>{translate(label)}</span>{translate(children)}{translate(hint && <small>{translate(hint)}</small>)}</label>;}
export default function TripForm({ trip, meta, onClose, onSaved, startStep = 0 }) {
  const [form, setForm] = useState(() => trip ? { ...structuredClone(trip), name: ko(trip.name), preferences: { ...structuredClone(trip.preferences), foods: trip.preferences.foods.map(ko), cuisine: ko(trip.preferences.cuisine) } } : defaultTrip());
  const [step, setStep] = useState(startStep),[error, setError] = useState(''),[saving, setSaving] = useState(false);
  const [foodsText, setFoodsText] = useState(form.preferences.foods.join(', '));
  const [alternativesText, setAlternativesText] = useState(form.alternativeDestinations.join(', '));
  const dialog = useRef(null);
  useEffect(() => {
    const focused = document.activeElement;
    dialog.current.showModal();
    return () => focused?.focus();
  }, []);
  const set = (key, value) => setForm((f) => ({ ...f, [key]: value }));
  const nest = (key, prop, value) => setForm((f) => ({ ...f, [key]: { ...f[key], [prop]: value } }));
  const weight = (key, value) => setForm((f) => ({ ...f, preferences: { ...f.preferences, weights: { ...f.preferences.weights, [key]: value } } }));
  async function submit(e) {
    e.preventDefault();setError('');
    if (step < 2) {
      if (step === 0 && (form.origin === form.destination || form.tripType === 'round_trip' && (!form.returnDate || form.returnDate < form.departureDate))) {setError('출발·도착 공항을 다르게 선택하고 귀국일을 출발일 이후로 설정해 주세요.');return;}
      if (step === 1 && (form.targetPrice <= 0 || form.maxPrice > 0 && form.targetPrice > form.maxPrice)) {setError('목표 요금은 0보다 크고 최대 요금 이하여야 합니다.');return;}
      setStep((s) => s + 1);return;
    }
    setSaving(true);
    try {
      const data = { ...form, preferences: { ...form.preferences, foods: foodsText.split(',').map((x) => x.trim()).filter(Boolean) }, alternativeDestinations: alternativesText.toUpperCase().split(',').map((x) => x.trim()).filter(Boolean), name: form.name.trim() || `${ko(meta.airports.find((a) => a.code === form.destination)?.city || form.destination)} 여행`, returnDate: form.tripType === 'one_way' ? null : form.returnDate };
      const saved = await api(trip ? `/trips/${trip.id}` : '/trips', { method: trip ? 'PUT' : 'POST', body: data });
      onSaved(saved);onClose();
    } catch (e) {setError(e.message);} finally {setSaving(false);}
  }
  return <dialog ref={dialog} className="trip-dialog" onCancel={(e) => {e.preventDefault();if (!saving) onClose();}}>
    <form onSubmit={submit}>
      <div className="dialog-head"><div><span className="eyebrow">{translate("\uC0C8\uB85C\uC6B4 \uC5EC\uD589\uC758 \uC2DC\uC791")}</span><h2>{translate(trip ? '내 취향에 맞게 수정하세요.' : '어디로 떠나볼까요?')}</h2></div><button type="button" className="icon-button" aria-label={translate("창 닫기")} onClick={onClose} disabled={saving}><X size={21} /></button></div>
      <div className="form-steps">{translate(['여행 정보', '가격·알림', '여행 취향'].map((label, i) => <button type="button" key={label} className={step === i ? 'active' : ''} onClick={() => {setStep(i);setError('');}}><span>{translate(i < step ? <Check size={13} /> : i + 1)}</span>{translate(label)}</button>))}</div>
      <div className="form-body">
      {translate(error && <div className="error-banner" role="alert">{translate(error)}</div>)}
      {translate(step === 0 && <>
        <div className="form-section-title"><Plane size={18} /><h3>{translate("\uC5EC\uD589\uC758 \uAE30\uBCF8 \uC815\uBCF4\uB97C \uC54C\uB824\uC8FC\uC138\uC694")}</h3></div>
        <Field label={translate("여행 이름")}><input autoFocus value={form.name} placeholder={translate("도쿄에서 보내는 긴 주말")} maxLength={80} onChange={(e) => set('name', e.target.value)} /></Field>
        <div className="field-grid">
          <Field label={translate("출발지 · 공항 코드")}><input required list="airport-options" value={form.origin} maxLength={3} pattern="[A-Z]{3}" onChange={(e) => set('origin', e.target.value.toUpperCase())} /></Field>
          <Field label={translate("도착지 · 공항 코드")}><input required list="airport-options" value={form.destination} maxLength={3} pattern="[A-Z]{3}" onChange={(e) => set('destination', e.target.value.toUpperCase())} /></Field>
        </div>
        <datalist id="airport-options">{translate(meta.airports.map((a) => <option key={a.code} value={a.code}>{translate(ko(a.city))} · {translate(ko(a.name))}</option>))}</datalist>
        <div className="segmented">{translate([['round_trip', '왕복'], ['one_way', '편도']].map(([value, label]) => <button type="button" key={value} onClick={() => set('tripType', value)} className={form.tripType === value ? 'active' : ''}>{translate(label)}</button>))}</div>
        <div className="field-grid"><Field label={translate("출발일")}><input required type="date" value={form.departureDate} onChange={(e) => set('departureDate', e.target.value)} /></Field><Field label={translate("귀국일")}><input required={form.tripType === 'round_trip'} type="date" min={form.departureDate} disabled={form.tripType === 'one_way'} value={form.returnDate || ''} onChange={(e) => set('returnDate', e.target.value)} /></Field></div>
        <div className="field-grid"><Field label={translate("여행 인원")}><select value={form.passengers} onChange={(e) => set('passengers', num(e))}><option value={1}>{translate("\uC131\uC778 1\uBA85")}</option><option value={2}>{translate("\uC131\uC778 2\uBA85")}</option></select></Field><Field label={translate("좌석 등급")}><select value={form.cabin} onChange={(e) => set('cabin', e.target.value)}>{translate(['ECONOMY', 'PREMIUM_ECONOMY', 'BUSINESS', 'FIRST'].map((c) => <option key={c} value={c}>{translate(LABELS[c])}</option>))}</select></Field></div>
        <label className="check-row"><input type="checkbox" checked={form.directOnly} onChange={(e) => set('directOnly', e.target.checked)} /><span>{translate("\uC9C1\uD56D\uB9CC \uAC80\uC0C9")}</span></label>
        <details className="form-details"><summary>{translate("\uC120\uD638 \uC2DC\uAC04\xB7\uC720\uC5F0\uD55C \uB0A0\uC9DC")}</summary><p>{translate("\uC120\uD638 \uC2DC\uAC04\uC740 \uAC00\uB294 \uD3B8\uC5D0 \uC801\uC6A9\uB418\uBA70 \uAC01 \uACF5\uD56D\uC758 \uD604\uC9C0 \uC2DC\uAC04 \uAE30\uC900\uC785\uB2C8\uB2E4.")}</p>
          {translate(['departureWindow', 'arrivalWindow'].map((k) => <div className="field-grid" key={k}><Field label={translate(`${k === 'departureWindow' ? '출발' : '도착'} · 시작 시각`)}><input type="time" required value={form[k].start} onChange={(e) => nest(k, 'start', e.target.value)} /></Field><Field label={translate("종료 시각")}><input type="time" required value={form[k].end} onChange={(e) => nest(k, 'end', e.target.value)} /></Field></div>))}
          <div className="field-grid">{translate(['flexDeparture', 'flexReturn'].map((k) => <Field label={translate(k === 'flexDeparture' ? '출발일 유연성' : '귀국일 유연성')} key={k}><select disabled={k === 'flexReturn' && form.tripType === 'one_way'} value={form[k]} onChange={(e) => set(k, num(e))}>{translate([0, 1, 3, 7].map((n) => <option key={n} value={n}>{translate(n ? `± ${n}일` : '지정 날짜만')}</option>))}</select></Field>))}</div>
          <label className="check-row"><input type="checkbox" checked={form.nearbyAirports} onChange={(e) => set('nearbyAirports', e.target.checked)} />{translate("\uC8FC\uBCC0 \uACF5\uD56D\uB3C4 \uD568\uAED8 \uAC80\uC0C9")}</label>
          <Field label={translate("대체 도착 공항")} hint={translate("IATA 공항 코드 최대 4개를 쉼표로 구분하세요.")}><input value={alternativesText} onChange={(e) => setAlternativesText(e.target.value.toUpperCase())} placeholder="HND, KIX" /></Field>
          <p className="quiet">{translate("\uD655\uC778\uD560 \uB54C\uB9C8\uB2E4 \uB0A0\uC9DC\xB7\uACF5\uD56D \uC870\uD569\uC744 \uCD5C\uB300 ")}{translate(meta.maxSearchVariants)}{translate("\uAC1C \uAC80\uC0C9\uD569\uB2C8\uB2E4. \uAC80\uC0C9 \uBC94\uC704\uAC00 \uB113\uC73C\uBA74 \uC870\uD68C \uBC94\uC704\uB97C \uC548\uB0B4\uD569\uB2C8\uB2E4.")}</p>
        </details>
      </>)}
      {translate(step === 1 && <>
        <div className="form-section-title"><SlidersHorizontal size={18} /><h3>{translate("\uC6D0\uD558\uB294 \uAC00\uACA9\uACFC \uC54C\uB9BC\uC744 \uC124\uC815\uD558\uC138\uC694")}</h3></div>
        <Field label={translate("요금·예산 기준")}><select value={form.priceBasis} onChange={(e) => set('priceBasis', e.target.value)}><option value="per_person">{translate("1\uC778 \uAE30\uC900 \xB7 \uC6D0")}</option><option value="total">{translate("\uC804\uCCB4 \uC778\uC6D0 \uAE30\uC900 \xB7 \uC6D0")}</option></select></Field>
        <div className="field-grid"><Field label={translate("알림 목표 요금 · 원")}><AmountInput required min={1} value={form.targetPrice} onValueChange={(value) => set('targetPrice', value)} /></Field><Field label={translate("최대 항공 요금 · 원")} hint={translate("0 또는 −1은 한도 없음입니다.")}><AmountInput value={form.maxPrice} onValueChange={(value) => set('maxPrice', value)} /></Field></div>
        <Field label={translate("항공권 검색 서비스")}><select value={form.provider} onChange={(e) => set('provider', e.target.value)}>{translate(meta.providers.map((p) => <option key={p.id} value={p.id} disabled={!p.configured}>{translate(ko(p.name))}{translate(!p.configured ? ' · 설정 필요' : '')}</option>))}</select></Field>
        {translate(form.provider === 'demo' && <div className="soft-note">{translate("\uB370\uBAA8 \uC694\uAE08\uC740 \uC608\uC2DC\uC785\uB2C8\uB2E4. \uC2E4\uC81C \uAC00\uACA9\uC744 \uBAA8\uB2C8\uD130\uB9C1\uD558\uB824\uBA74 \uC6B4\uC601\uC6A9 \uD56D\uACF5\uAD8C \uAC80\uC0C9 \uC11C\uBE44\uC2A4\uB97C \uC5F0\uACB0\uD574 \uC8FC\uC138\uC694.")}</div>)}
        <div className="field-grid"><Field label={translate("가격 확인 주기")}><select value={form.schedule.mode} onChange={(e) => nest('schedule', 'mode', e.target.value)}><option value="daily">{translate("\uD558\uB8E8 1\uD68C")}</option><option value="twice_daily">{translate("\uD558\uB8E8 2\uD68C")}</option><option value="interval">{translate("\uC2DC\uAC04 \uAC04\uACA9 \uC124\uC815")}</option></select></Field><Field label={translate("시간대")}><input required value={form.schedule.timezone} onChange={(e) => nest('schedule', 'timezone', e.target.value)} /></Field></div>
        {translate(form.schedule.mode === 'interval' ? <Field label={translate("확인 간격 · 시간")}><input type="number" required min={1} max={168} value={form.schedule.intervalHours} onChange={(e) => nest('schedule', 'intervalHours', num(e))} /></Field> : <div className="field-grid"><Field label={translate("첫 번째 확인 시각")}><input type="time" required value={form.schedule.times[0]} onChange={(e) => nest('schedule', 'times', [e.target.value, form.schedule.times[1] || '17:30'])} /></Field>{translate(form.schedule.mode === 'twice_daily' && <Field label={translate("두 번째 확인 시각")}><input type="time" required value={form.schedule.times[1] || '17:30'} onChange={(e) => nest('schedule', 'times', [form.schedule.times[0], e.target.value])} /></Field>)}</div>)}
        <h4>{translate("\uB2E4\uC74C \uC870\uAC74\uC5D0\uC11C \uC54C\uB824\uC8FC\uC138\uC694")}</h4>
        <label className="check-row"><input type="checkbox" checked={form.alertRules.target} onChange={(e) => nest('alertRules', 'target', e.target.checked)} />{translate("\uC694\uAE08\uC774 \uBAA9\uD45C\uAC00 \uC774\uD558\uC77C \uB54C")}</label>
        <label className="check-row"><input type="checkbox" checked={form.alertRules.lowest} onChange={(e) => nest('alertRules', 'lowest', e.target.checked)} />{translate("\uC0C8\uB85C\uC6B4 \uCD5C\uC800\uAC00\uB97C \uCC3E\uC558\uC744 \uB54C")}</label>
        <label className="check-row"><input type="checkbox" checked={form.alertRules.drop} onChange={(e) => nest('alertRules', 'drop', e.target.checked)} />{translate("\uC694\uAE08\uC774 \uC774\uC804 \uB300\uBE44 ")}<input className="inline-input" aria-label={translate("가격 하락 비율")} type="number" min={1} max={100} value={form.alertRules.dropPercent} onChange={(e) => nest('alertRules', 'dropPercent', num(e))} />{translate("% \uC774\uC0C1 \uD558\uB77D\uD588\uC744 \uB54C")}</label>
        <p className="quiet">{translate("\uC54C\uB9BC\uC740 \uC571 \uC548\uC5D0 \uD45C\uC2DC\uB429\uB2C8\uB2E4. \uC790\uB3D9 \uD655\uC778\uC744 \uC704\uD574 \uC11C\uBC84\uB97C \uACC4\uC18D \uC2E4\uD589\uD574 \uC8FC\uC138\uC694.")}</p>
      </>)}
      {translate(step === 2 && <>
        <div className="form-section-title"><Sparkles size={18} /><h3>{translate("\uC5B4\uB5A4 \uC5EC\uD589\uC744 \uC88B\uC544\uD558\uC2DC\uB098\uC694")}</h3></div>
        <Field label={translate("누구와 떠나나요?")}><select value={form.preferences.companion} onChange={(e) => nest('preferences', 'companion', e.target.value)}><option value="solo">{translate("\uD63C\uC790 \uC5EC\uD589")}</option><option value="couple">{translate("\uCEE4\uD50C \uC5EC\uD589")}</option><option value="family">{translate("\uAC00\uC871 \uC5EC\uD589")}</option></select></Field>
        <div className="weight-grid">{translate(styles.map((s) => <label key={s} className="weight"><span>{translate(LABELS[s])}<b>{translate(form.preferences.weights[s] || 0)}</b></span><input type="range" min={0} max={100} step={5} value={form.preferences.weights[s] || 0} onChange={(e) => weight(s, num(e))} /></label>))}</div>
        <p className="quiet">{translate("\uC810\uC218\uB294 \uC0C1\uB300\uC801\uC778 \uC6B0\uC120\uC21C\uC704\uC785\uB2C8\uB2E4. \uD569\uACC4\uAC00 100\uC77C \uD544\uC694\uB294 \uC5C6\uC5B4\uC694.")}</p>
        <div className="field-grid"><Field label={translate("먹고 싶은 음식 · 쉼표로 구분")}><input value={foodsText} onChange={(e) => setFoodsText(e.target.value)} /></Field><Field label={translate("선호하는 요리")}><input value={form.preferences.cuisine} onChange={(e) => nest('preferences', 'cuisine', e.target.value)} /></Field></div>
        <Field label={translate("한 끼 예상 한도 · 1인 기준 원")}><AmountInput value={form.preferences.mealBudget} onValueChange={(value) => nest('preferences', 'mealBudget', value)} /></Field>
        <h4>{translate("\uC5EC\uD589 \uC608\uC0B0\uC744 \uC124\uC815\uD558\uC138\uC694 \xB7 \uC6D0 ")}{translate(form.priceBasis === 'per_person' ? '1인 기준' : '전체 인원 기준')}</h4>
        <div className="field-grid">{translate(Object.keys(form.budget).map((k) => <Field label={translate(`${k === 'food' ? '식비' : LABELS[k] || k} ${k === 'total' || k === 'flight' ? '· 필수' : '· 선택'}`)} key={k}><AmountInput required={k === 'total' || k === 'flight'} value={form.budget[k]} onValueChange={(value) => nest('budget', k, value)} /></Field>))}</div>
        <p className="quiet">{translate("0 \uB610\uB294 \u22121\uC740 \uC608\uC0B0 \uD55C\uB3C4\uB97C \uD574\uC81C\uD569\uB2C8\uB2E4. \uC77C\uC815\uC5D0\uB294 \uD569\uB9AC\uC801\uC778 \uC608\uC0C1 \uBE44\uC6A9\uC744 \uC0AC\uC6A9\uD569\uB2C8\uB2E4. \uC219\uC18C \uAC80\uC0C9\uC740 \uCD94\uD6C4 \uC81C\uACF5\uD558\uBA70 \uD604\uC7AC \uC219\uBC15 \uAE08\uC561\uC740 \uD655\uBCF4\uD574 \uB458 \uC608\uC0B0\uC785\uB2C8\uB2E4.")}</p>
        <Field label={translate("추가 요청")}><textarea rows={2} maxLength={1000} value={form.preferences.notes} onChange={(e) => nest('preferences', 'notes', e.target.value)} placeholder={translate("여유로운 일정, 커피를 즐길 시간…")} /></Field>
      </>)}
      </div>
      <div className="dialog-footer"><button type="button" className="button secondary" onClick={() => step ? setStep((s) => s - 1) : onClose()} disabled={saving}><ChevronLeft size={16} />{translate(step ? '이전' : '취소')}</button><button className="button primary" type="submit" disabled={saving}>{translate(saving ? '저장 중…' : step === 2 ? trip ? '수정 저장' : '여행 모니터링 시작' : '다음')}{translate(step === 2 ? <Check size={16} /> : <ArrowRight size={16} />)}</button></div>
    </form>
  </dialog>;
}

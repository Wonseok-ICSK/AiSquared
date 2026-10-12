import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Check, ChevronLeft, Plane, SlidersHorizontal, Sparkles, X } from 'lucide-react';
import { api } from './api.js';
import AmountInput from './AmountInput.jsx';
import { ko, LABELS } from '../shared/ko.js';

const styles = ['sightseeing', 'food', 'relaxation', 'resort', 'shopping', 'nightlife', 'nature', 'photography', 'culture', 'local'];
export function defaultTrip() {
  const d = new Date(); d.setDate(20); d.setMonth(d.getMonth() + 1);
  const departureDate = d.toLocaleDateString('en-CA'); d.setDate(23);
  return { name: '', origin: 'ICN', destination: 'NRT', departureDate, returnDate: d.toLocaleDateString('en-CA'), tripType: 'round_trip', passengers: 2, cabin: 'ECONOMY', directOnly: true, departureWindow: { start: '00:00', end: '23:59' }, arrivalWindow: { start: '00:00', end: '23:59' }, flexDeparture: 0, flexReturn: 0, nearbyAirports: false, alternativeDestinations: [], provider: 'google_flights', targetPrice: 300000, maxPrice: 450000, priceBasis: 'per_person', schedule: { mode: 'daily', times: ['08:30', '17:30'], timezone: 'Asia/Seoul', intervalHours: 6 }, alertRules: { target: true, drop: false, dropPercent: 10, lowest: true }, preferences: { companion: 'couple', weights: Object.fromEntries(styles.map(s => [s, s === 'food' || s === 'sightseeing' ? 40 : s === 'shopping' ? 20 : 0])), foods: ['스시', '라멘', '야키토리'], cuisine: '현지 음식', mealBudget: 30000, notes: '' }, budget: { total: 1500000, flight: 350000, hotel: 300000, food: 180000, transport: 70000, shopping: 100000, activities: 80000, other: 30000 } };
}
const num = e => Number(e.target.value);
function Field({ label, children, hint, className = '' }) { return <label className={`field ${className}`}><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>; }
export default function TripForm({ trip, meta, onClose, onSaved, startStep = 0 }) {
  const [form, setForm] = useState(() => trip ? { ...structuredClone(trip), name: ko(trip.name), preferences: { ...structuredClone(trip.preferences), foods: trip.preferences.foods.map(ko), cuisine: ko(trip.preferences.cuisine) } } : defaultTrip());
  const [step, setStep] = useState(startStep), [error, setError] = useState(''), [saving, setSaving] = useState(false);
  const [foodsText, setFoodsText] = useState(form.preferences.foods.join(', '));
  const [alternativesText, setAlternativesText] = useState(form.alternativeDestinations.join(', '));
  const dialog = useRef(null);
  useEffect(() => {
    const focused = document.activeElement;
    dialog.current.showModal();
    return () => focused?.focus();
  }, []);
  const set = (key, value) => setForm(f => ({ ...f, [key]: value }));
  const nest = (key, prop, value) => setForm(f => ({ ...f, [key]: { ...f[key], [prop]: value } }));
  const weight = (key, value) => setForm(f => ({ ...f, preferences: { ...f.preferences, weights: { ...f.preferences.weights, [key]: value } } }));
  async function submit(e) {
    e.preventDefault(); setError('');
    if (step < 2) {
      if (step === 0 && (form.origin === form.destination || (form.tripType === 'round_trip' && (!form.returnDate || form.returnDate < form.departureDate)))) { setError('출발·도착 공항을 다르게 선택하고 귀국일을 출발일 이후로 설정해 주세요.'); return; }
      if (step === 1 && (form.targetPrice <= 0 || (form.maxPrice > 0 && form.targetPrice > form.maxPrice))) { setError('목표 요금은 0보다 크고 최대 요금 이하여야 합니다.'); return; }
      setStep(s => s + 1); return;
    }
    setSaving(true);
    try {
      const data = { ...form, preferences: { ...form.preferences, foods: foodsText.split(',').map(x => x.trim()).filter(Boolean) }, alternativeDestinations: alternativesText.toUpperCase().split(',').map(x => x.trim()).filter(Boolean), name: form.name.trim() || `${ko(meta.airports.find(a => a.code === form.destination)?.city || form.destination)} 여행`, returnDate: form.tripType === 'one_way' ? null : form.returnDate };
      const saved = await api(trip ? `/trips/${trip.id}` : '/trips', { method: trip ? 'PUT' : 'POST', body: data });
      onSaved(saved); onClose();
    } catch (e) { setError(e.message); } finally { setSaving(false); }
  }
  return <dialog ref={dialog} className="trip-dialog" onCancel={e => { e.preventDefault(); if (!saving) onClose(); }}>
    <form onSubmit={submit}>
      <div className="dialog-head"><div><span className="eyebrow">새로운 여행의 시작</span><h2>{trip ? '내 취향에 맞게 수정하세요.' : '어디로 떠나볼까요?'}</h2></div><button type="button" className="icon-button" aria-label="창 닫기" onClick={onClose} disabled={saving}><X size={21}/></button></div>
      <div className="form-steps">{['여행 정보', '가격·알림', '여행 취향'].map((label, i) => <button type="button" key={label} className={step === i ? 'active' : ''} onClick={() => { setStep(i); setError(''); }}><span>{i < step ? <Check size={13}/> : i + 1}</span>{label}</button>)}</div>
      <div className="form-body">
      {error && <div className="error-banner" role="alert">{error}</div>}
      {step === 0 && <>
        <div className="form-section-title"><Plane size={18}/><h3>여행의 기본 정보를 알려주세요</h3></div>
        <Field label="여행 이름"><input autoFocus value={form.name} placeholder="도쿄에서 보내는 긴 주말" maxLength={80} onChange={e => set('name', e.target.value)}/></Field>
        <div className="field-grid">
          <Field label="출발지 · 공항 코드"><input required list="airport-options" value={form.origin} maxLength={3} pattern="[A-Z]{3}" onChange={e => set('origin', e.target.value.toUpperCase())}/></Field>
          <Field label="도착지 · 공항 코드"><input required list="airport-options" value={form.destination} maxLength={3} pattern="[A-Z]{3}" onChange={e => set('destination', e.target.value.toUpperCase())}/></Field>
        </div>
        <datalist id="airport-options">{meta.airports.map(a => <option key={a.code} value={a.code}>{ko(a.city)} · {ko(a.name)}</option>)}</datalist>
        <div className="segmented">{[['round_trip', '왕복'], ['one_way', '편도']].map(([value, label]) => <button type="button" key={value} onClick={() => set('tripType', value)} className={form.tripType === value ? 'active' : ''}>{label}</button>)}</div>
        <div className="field-grid"><Field label="출발일"><input required type="date" value={form.departureDate} onChange={e => set('departureDate', e.target.value)}/></Field><Field label="귀국일"><input required={form.tripType === 'round_trip'} type="date" min={form.departureDate} disabled={form.tripType === 'one_way'} value={form.returnDate || ''} onChange={e => set('returnDate', e.target.value)}/></Field></div>
        <div className="field-grid"><Field label="여행 인원"><select value={form.passengers} onChange={e => set('passengers', num(e))}><option value={1}>성인 1명</option><option value={2}>성인 2명</option></select></Field><Field label="좌석 등급"><select value={form.cabin} onChange={e => set('cabin', e.target.value)}>{['ECONOMY', 'PREMIUM_ECONOMY', 'BUSINESS', 'FIRST'].map(c => <option key={c} value={c}>{LABELS[c]}</option>)}</select></Field></div>
        <label className="check-row"><input type="checkbox" checked={form.directOnly} onChange={e => set('directOnly', e.target.checked)}/><span>직항만 검색</span></label>
        <details className="form-details"><summary>선호 시간·유연한 날짜</summary><p>선호 시간은 가는 편에 적용되며 각 공항의 현지 시간 기준입니다.</p>
          {['departureWindow', 'arrivalWindow'].map(k => <div className="field-grid" key={k}><Field label={`${k === 'departureWindow' ? '출발' : '도착'} · 시작 시각`}><input type="time" required value={form[k].start} onChange={e => nest(k, 'start', e.target.value)}/></Field><Field label="종료 시각"><input type="time" required value={form[k].end} onChange={e => nest(k, 'end', e.target.value)}/></Field></div>)}
          <div className="field-grid">{['flexDeparture', 'flexReturn'].map(k => <Field label={k === 'flexDeparture' ? '출발일 유연성' : '귀국일 유연성'} key={k}><select disabled={k === 'flexReturn' && form.tripType === 'one_way'} value={form[k]} onChange={e => set(k, num(e))}>{[0, 1, 3, 7].map(n => <option key={n} value={n}>{n ? `± ${n}일` : '지정 날짜만'}</option>)}</select></Field>)}</div>
          <label className="check-row"><input type="checkbox" checked={form.nearbyAirports} onChange={e => set('nearbyAirports', e.target.checked)}/>주변 공항도 함께 검색</label>
          <Field label="대체 도착 공항" hint="IATA 공항 코드 최대 4개를 쉼표로 구분하세요."><input value={alternativesText} onChange={e => setAlternativesText(e.target.value.toUpperCase())} placeholder="HND, KIX"/></Field>
          <p className="quiet">확인할 때마다 날짜·공항 조합을 최대 {meta.maxSearchVariants}개 검색합니다. 검색 범위가 넓으면 조회 범위를 안내합니다.</p>
        </details>
      </>}
      {step === 1 && <>
        <div className="form-section-title"><SlidersHorizontal size={18}/><h3>원하는 가격과 알림을 설정하세요</h3></div>
        <Field label="요금·예산 기준"><select value={form.priceBasis} onChange={e => set('priceBasis', e.target.value)}><option value="per_person">1인 기준 · 원</option><option value="total">전체 인원 기준 · 원</option></select></Field>
        <div className="field-grid"><Field label="알림 목표 요금 · 원"><AmountInput required min={1} value={form.targetPrice} onValueChange={value => set('targetPrice', value)}/></Field><Field label="최대 항공 요금 · 원" hint="0 또는 −1은 한도 없음입니다."><AmountInput value={form.maxPrice} onValueChange={value => set('maxPrice', value)}/></Field></div>
        <Field label="항공권 검색 서비스"><select value={form.provider} onChange={e => set('provider', e.target.value)}>{meta.providers.map(p => <option key={p.id} value={p.id} disabled={!p.configured}>{ko(p.name)}{!p.configured ? ' · 설정 필요' : ''}</option>)}</select></Field>
        {form.provider === 'demo' && <div className="soft-note">데모 요금은 예시입니다. 실제 가격을 모니터링하려면 운영용 항공권 검색 서비스를 연결해 주세요.</div>}
        <div className="field-grid"><Field label="가격 확인 주기"><select value={form.schedule.mode} onChange={e => nest('schedule', 'mode', e.target.value)}><option value="daily">하루 1회</option><option value="twice_daily">하루 2회</option><option value="interval">시간 간격 설정</option></select></Field><Field label="시간대"><input required value={form.schedule.timezone} onChange={e => nest('schedule', 'timezone', e.target.value)}/></Field></div>
        {form.schedule.mode === 'interval' ? <Field label="확인 간격 · 시간"><input type="number" required min={1} max={168} value={form.schedule.intervalHours} onChange={e => nest('schedule', 'intervalHours', num(e))}/></Field> : <div className="field-grid"><Field label="첫 번째 확인 시각"><input type="time" required value={form.schedule.times[0]} onChange={e => nest('schedule', 'times', [e.target.value, form.schedule.times[1] || '17:30'])}/></Field>{form.schedule.mode === 'twice_daily' && <Field label="두 번째 확인 시각"><input type="time" required value={form.schedule.times[1] || '17:30'} onChange={e => nest('schedule', 'times', [form.schedule.times[0], e.target.value])}/></Field>}</div>}
        <h4>다음 조건에서 알려주세요</h4>
        <label className="check-row"><input type="checkbox" checked={form.alertRules.target} onChange={e => nest('alertRules', 'target', e.target.checked)}/>요금이 목표가 이하일 때</label>
        <label className="check-row"><input type="checkbox" checked={form.alertRules.lowest} onChange={e => nest('alertRules', 'lowest', e.target.checked)}/>새로운 최저가를 찾았을 때</label>
        <label className="check-row"><input type="checkbox" checked={form.alertRules.drop} onChange={e => nest('alertRules', 'drop', e.target.checked)}/>요금이 이전 대비 <input className="inline-input" aria-label="가격 하락 비율" type="number" min={1} max={100} value={form.alertRules.dropPercent} onChange={e => nest('alertRules', 'dropPercent', num(e))}/>% 이상 하락했을 때</label>
        <p className="quiet">알림은 앱 안에 표시됩니다. 자동 확인을 위해 서버를 계속 실행해 주세요.</p>
      </>}
      {step === 2 && <>
        <div className="form-section-title"><Sparkles size={18}/><h3>어떤 여행을 좋아하시나요</h3></div>
        <Field label="누구와 떠나나요?"><select value={form.preferences.companion} onChange={e => nest('preferences', 'companion', e.target.value)}><option value="solo">혼자 여행</option><option value="couple">커플 여행</option><option value="family">가족 여행</option></select></Field>
        <div className="weight-grid">{styles.map(s => <label key={s} className="weight"><span>{LABELS[s]}<b>{form.preferences.weights[s] || 0}</b></span><input type="range" min={0} max={100} step={5} value={form.preferences.weights[s] || 0} onChange={e => weight(s, num(e))}/></label>)}</div>
        <p className="quiet">점수는 상대적인 우선순위입니다. 합계가 100일 필요는 없어요.</p>
        <div className="field-grid"><Field label="먹고 싶은 음식 · 쉼표로 구분"><input value={foodsText} onChange={e => setFoodsText(e.target.value)}/></Field><Field label="선호하는 요리"><input value={form.preferences.cuisine} onChange={e => nest('preferences', 'cuisine', e.target.value)}/></Field></div>
        <Field label="한 끼 예상 한도 · 1인 기준 원"><AmountInput value={form.preferences.mealBudget} onValueChange={value => nest('preferences', 'mealBudget', value)}/></Field>
        <h4>여행 예산을 설정하세요 · 원 {form.priceBasis === 'per_person' ? '1인 기준' : '전체 인원 기준'}</h4>
        <div className="field-grid">{Object.keys(form.budget).map(k => <Field label={`${k === 'food' ? '식비' : LABELS[k] || k} ${k === 'total' || k === 'flight' ? '· 필수' : '· 선택'}`} key={k}><AmountInput required={k === 'total' || k === 'flight'} value={form.budget[k]} onValueChange={value => nest('budget', k, value)}/></Field>)}</div>
        <p className="quiet">0 또는 −1은 예산 한도를 해제합니다. 일정에는 합리적인 예상 비용을 사용합니다. 숙소 검색은 추후 제공하며 현재 숙박 금액은 확보해 둘 예산입니다.</p>
        <Field label="추가 요청"><textarea rows={2} maxLength={1000} value={form.preferences.notes} onChange={e => nest('preferences', 'notes', e.target.value)} placeholder="여유로운 일정, 커피를 즐길 시간…"/></Field>
      </>}
      </div>
      <div className="dialog-footer"><button type="button" className="button secondary" onClick={() => step ? setStep(s => s - 1) : onClose()} disabled={saving}><ChevronLeft size={16}/>{step ? '이전' : '취소'}</button><button className="button primary" type="submit" disabled={saving}>{saving ? '저장 중…' : step === 2 ? trip ? '수정 저장' : '여행 모니터링 시작' : '다음'}{step === 2 ? <Check size={16}/> : <ArrowRight size={16}/>}</button></div>
    </form>
  </dialog>;
}

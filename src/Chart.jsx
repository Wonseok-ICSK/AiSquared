import { useState } from 'react';
import { money, shortDate } from './api.js';

export default function PriceChart({ trip }) {
  const [hover, setHover] = useState(null);
  const data = trip.history.filter(h => h.price != null);
  if (!data.length) return <div className="chart-empty"><span className="empty-orbit">↗</span><p>가격 기록을 시작해 보세요.</p><span>가격 확인을 누르면 첫 기록이 표시됩니다.</span></div>;
  const width = 820, height = 230, left = 54, right = 22, top = 24, bottom = 36;
  const values = [...data.map(h => h.price), trip.targetPrice];
  const min = Math.floor(Math.min(...values) * .92 / 10000) * 10000;
  const max = Math.ceil(Math.max(...values) * 1.04 / 10000) * 10000;
  const x = i => left + (width - left - right) * (data.length === 1 ? .5 : i / (data.length - 1));
  const y = price => top + (max - price) / (max - min || 1) * (height - top - bottom);
  const points = data.map((p, i) => `${x(i)},${y(p.price)}`).join(' ');
  const line = data.map((p, i) => `${i ? 'L' : 'M'}${x(i)} ${y(p.price)}`).join(' ');
  const area = `${line} L${x(data.length - 1)} ${height - bottom} L${x(0)} ${height - bottom} Z`;
  const shown = hover ?? data.length - 1;
  return <div className="chart-wrap">
    <div className="chart-legend"><span><i className="legend-dot" /> 조건에 맞는 최저 요금</span><span><i className="legend-dash" /> 목표 요금</span><span className="chart-current">{shortDate(data[shown].checkedAt)} <b>{money(data[shown].price)}</b></span></div>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`가격 기록: ${data.map(h => `${shortDate(h.checkedAt)} ${money(h.price)}`).join(', ')}. 목표 요금 ${money(trip.targetPrice)}.`}>
      <defs><linearGradient id="price-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#3a7762" stopOpacity=".18"/><stop offset="100%" stopColor="#3a7762" stopOpacity=".015"/></linearGradient></defs>
      {[0, 1, 2, 3].map(i => { const price = min + (max - min) / 3 * i; return <g key={i}><line x1={left} x2={width - right} y1={y(price)} y2={y(price)} stroke="#eceee9" strokeDasharray="3 4"/><text x={left - 12} y={y(price) + 4} textAnchor="end" fill="#859089" fontSize="11">{(price / 10000).toFixed(1)}만</text></g>; })}
      <path d={area} fill="url(#price-fill)"/>
      <line x1={left} x2={width - right} y1={y(trip.targetPrice)} y2={y(trip.targetPrice)} stroke="#b9a179" strokeDasharray="5 5"/>
      <polyline points={points} fill="none" stroke="#2e705b" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round"/>
      {data.map((point, i) => <g key={point.id} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
        <circle cx={x(i)} cy={y(point.price)} r={shown === i ? 5 : 3.5} fill="#fff" stroke="#2e705b" strokeWidth="2"/>
        <circle cx={x(i)} cy={y(point.price)} r="16" fill="transparent"/>
        {(data.length <= 10 || i % Math.ceil(data.length / 8) === 0 || i === data.length - 1) && <text x={x(i)} y={height - 10} textAnchor="middle" fill="#859089" fontSize="11">{shortDate(point.checkedAt)}</text>}
      </g>)}
    </svg>
  </div>;
}

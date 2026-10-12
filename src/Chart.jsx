import { translate } from "./i18n.js";import { useState } from 'react';
import { money, shortDate } from './api.js';

export default function PriceChart({ trip }) {
  const [hover, setHover] = useState(null);
  const data = trip.history.filter((h) => h.price != null);
  if (!data.length) return <div className="chart-empty"><span className="empty-orbit">↗</span><p>{translate("\uAC00\uACA9 \uAE30\uB85D\uC744 \uC2DC\uC791\uD574 \uBCF4\uC138\uC694.")}</p><span>{translate("\uAC00\uACA9 \uD655\uC778\uC744 \uB204\uB974\uBA74 \uCCAB \uAE30\uB85D\uC774 \uD45C\uC2DC\uB429\uB2C8\uB2E4.")}</span></div>;
  const width = 820,height = 230,left = 54,right = 22,top = 24,bottom = 36;
  const values = [...data.map((h) => h.price), trip.targetPrice];
  const min = Math.floor(Math.min(...values) * .92 / 10000) * 10000;
  const max = Math.ceil(Math.max(...values) * 1.04 / 10000) * 10000;
  const x = (i) => left + (width - left - right) * (data.length === 1 ? .5 : i / (data.length - 1));
  const y = (price) => top + (max - price) / (max - min || 1) * (height - top - bottom);
  const points = data.map((p, i) => `${x(i)},${y(p.price)}`).join(' ');
  const line = data.map((p, i) => `${i ? 'L' : 'M'}${x(i)} ${y(p.price)}`).join(' ');
  const area = `${line} L${x(data.length - 1)} ${height - bottom} L${x(0)} ${height - bottom} Z`;
  const shown = hover ?? data.length - 1;
  return <div className="chart-wrap">
    <div className="chart-legend"><span><i className="legend-dot" />{translate(" \uC870\uAC74\uC5D0 \uB9DE\uB294 \uCD5C\uC800 \uC694\uAE08")}</span><span><i className="legend-dash" />{translate(" \uBAA9\uD45C \uC694\uAE08")}</span><span className="chart-current">{translate(shortDate(data[shown].checkedAt))} <b>{translate(money(data[shown].price))}</b></span></div>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={translate(`가격 기록: ${data.map((h) => `${shortDate(h.checkedAt)} ${money(h.price)}`).join(', ')}. 목표 요금 ${money(trip.targetPrice)}.`)}>
      <defs><linearGradient id="price-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#242d8d" stopOpacity=".18" /><stop offset="100%" stopColor="#242d8d" stopOpacity=".015" /></linearGradient></defs>
      {translate([0, 1, 2, 3].map((i) => {const price = min + (max - min) / 3 * i;return <g key={i}><line x1={left} x2={width - right} y1={y(price)} y2={y(price)} stroke="#e6e9f1" strokeDasharray="3 4" /><text x={left - 12} y={y(price) + 4} textAnchor="end" fill="#797d9c" fontSize="11">{translate((price / 10000).toFixed(1))}{translate("\uB9CC")}</text></g>;}))}
      <path d={area} fill="url(#price-fill)" />
      <line x1={left} x2={width - right} y1={y(trip.targetPrice)} y2={y(trip.targetPrice)} stroke="#b9a179" strokeDasharray="5 5" />
      <polyline points={points} fill="none" stroke="#1e2680" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
      {translate(data.map((point, i) => <g key={point.id} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
        <circle cx={x(i)} cy={y(point.price)} r={shown === i ? 5 : 3.5} fill="#fff" stroke="#1e2680" strokeWidth="2" />
        <circle cx={x(i)} cy={y(point.price)} r="16" fill="transparent" />
        {translate((data.length <= 10 || i % Math.ceil(data.length / 8) === 0 || i === data.length - 1) && <text x={x(i)} y={height - 10} textAnchor="middle" fill="#797d9c" fontSize="11">{translate(shortDate(point.checkedAt))}</text>)}
      </g>))}
    </svg>
  </div>;
}

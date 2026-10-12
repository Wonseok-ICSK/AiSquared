import { useLayoutEffect, useRef, useState } from 'react';

const format = value => String(value ?? '').replace(/\B(?=(\d{3})+(?!\d))/g, ',');

export default function AmountInput({ value, onValueChange, min = -1, max = 1000000000, ...props }) {
  const input = useRef(null), caret = useRef(null);
  const [draft, setDraft] = useState(null);
  const text = draft ?? format(value);

  useLayoutEffect(() => {
    const amount = Number(text.replaceAll(',', ''));
    const message = text === '-' || !Number.isSafeInteger(amount) ? '금액을 정수로 입력해 주세요.'
      : text && amount < min ? `${format(min)}원 이상 입력해 주세요.`
      : text && amount > max ? `${format(max)}원 이하로 입력해 주세요.` : '';
    input.current.setCustomValidity(message);
    if (caret.current !== null) {
      input.current.setSelectionRange(caret.current, caret.current);
      caret.current = null;
    }
  }, [text, min, max]);

  function change(event) {
    const entered = event.target.value;
    const raw = entered.replace(/[,\s]/g, '');
    if (!/^-?\d*$/.test(raw)) return;
    const normalized = raw.replace(/^(-?)0+(?=\d)/, '$1');
    const formatted = format(normalized);
    const charactersBeforeCaret = entered.slice(0, event.target.selectionStart).replace(/[,\s]/g, '').length;
    let position = 0, count = 0;
    while (position < formatted.length && count < charactersBeforeCaret) {
      if (formatted[position] !== ',') count++;
      position++;
    }
    caret.current = position;
    setDraft(formatted);
    // Keep API requests and saved budgets numeric; commas are only for display.
    onValueChange(raw === '' || raw === '-' ? 0 : Number(raw));
  }

  return <input {...props} ref={input} type="text" inputMode="numeric" value={text}
    onChange={change} onBlur={() => setDraft(null)}/>;
}

import React from 'react';
import {AbsoluteFill, Audio, interpolate, useCurrentFrame} from 'remotion';
import {Bar, Fonts, Grain, INK, M, MUTE, Sfx, Stage, camState, clamp, clampCam, out3} from './common';

/* «Детали картины»: хук на крупной детали, рассказ со словами по голосу, над строкой — номер и имя детали
   («02 — The letter»), кульминация антиквой, финал — камера сама ужимает картину в этикетку.
   Рамок вокруг деталей нет: указывает камера, а края кадра чуть приглушены. */

type Wd = {w: string; em: boolean; t: number};

// группа на экране — предложение; длинное делится на запятой после 7 слов или на 11-м слове
function groups(words: Wd[]) {
  const out: Wd[][] = []; let cur: Wd[] = [];
  words.forEach((w) => {
    cur.push(w);
    if ((/[.!?…]$/.test(w.w) && cur.length >= 3) || (/[,;:—]$/.test(w.w) && cur.length >= 7) || cur.length >= 11) {
      out.push(cur); cur = [];
    }
  });
  if (cur.length) out.push(cur);
  return out;
}

const Word: React.FC<{w: Wd; t: number; serif?: boolean; mute?: boolean; instant?: boolean; size?: number}> =
  ({w, t, serif, mute, instant, size}) => {
  const k = instant ? (t >= w.t - 0.02 ? 1 : 0) : interpolate(t, [w.t - 0.04, w.t + 0.24], [0, 1], out3);
  return <span style={{display: 'inline-block', opacity: k, transform: `translateY(${(1 - k) * 14}px)`,
    marginRight: '0.26em', color: w.em ? INK : mute ? MUTE : INK, fontFamily: w.em || serif ? 'Serif' : 'Grot',
    fontStyle: w.em ? 'italic' : 'normal', fontWeight: w.em || serif ? 400 : 480,
    fontSize: w.em && !serif && size ? size * 1.2 : undefined}}>{w.w}</span>;
};

export const Story: React.FC<any> = (p) => {
  const frame = useCurrentFrame(); const t = frame / p.fps;
  const st = camState(p.cam, t);
  const fix = (c: [number, number, number]) => t < p.endStart ? clampCam(p.pw, p.ph, c, p.slack) : c;
  const cam = fix(st.cam);
  // скорость картины на экране за кадр → смаз по осям (как выдержка камеры в полкадра)
  const nx = fix(camState(p.cam, t + 1 / p.fps).cam);
  const sc = 1080 / cam[2];
  const mb: [number, number] = t < p.endStart && !st.prev
    ? [Math.min(9, Math.abs(nx[0] - cam[0]) * sc * 0.3), Math.min(9, Math.abs(nx[1] - cam[1]) * sc * 0.3)] : [0, 0];
  const endK = interpolate(t, [p.endStart, p.endStart + 0.8], [0, 1], clamp);
  const beat = p.beats.find((b: any) => t >= b.start - 0.1 && t < b.end) || p.beats[p.beats.length - 1];
  // приглушённые края — только пока камера стоит на детали
  const det = p.beats.find((b: any) => (b.kind === 'reveal' || b.kind === 'climax') && t >= b.arrive - 0.2 && t < b.end);
  const vig = det ? Math.min(interpolate(t, [det.arrive - 0.2, det.arrive + 0.6], [0, 1], out3),
    interpolate(t, [det.end - 0.4, det.end], [1, 0], clamp)) : 0;

  return <AbsoluteFill style={{background: '#0d0c0b', color: INK, overflow: 'hidden'}}>
    <Fonts />
    {st.prev && st.mix < 1 && <Stage src={p.image} pw={p.pw} ph={p.ph} cam={fix(st.prev)} />}
    <div style={{position: 'absolute', inset: 0, opacity: st.mix}}>
      <Stage src={p.image} pw={p.pw} ph={p.ph} cam={cam} shadow={endK} blur={mb} />
    </div>
    <div style={{position: 'absolute', inset: 0, opacity: vig * (1 - endK),
      background: 'radial-gradient(ellipse 75% 45% at 50% 40%, rgba(8,7,6,0) 55%, rgba(8,7,6,.55) 100%)'}} />
    {/* затемнения под текст; на титре уходят */}
    <div style={{position: 'absolute', inset: 0, opacity: 1 - endK}}>
      <div style={{position: 'absolute', left: 0, right: 0, top: 0, height: beat.kind === 'hook' ? 900 : 420,
        background: 'linear-gradient(to bottom,rgba(8,7,6,.82),rgba(8,7,6,.4) 55%,rgba(8,7,6,0))'}} />
      <div style={{position: 'absolute', left: 0, right: 0, bottom: 0, height: beat.kind === 'climax' ? 1000 : 900,
        background: 'linear-gradient(to top,rgba(8,7,6,.9),rgba(8,7,6,.6) 42%,rgba(8,7,6,0))'}} />
    </div>

    <Bar text={p.beats.indexOf(beat) === 0 ? p.rubric : p.series} opacity={1 - endK} />

    {/* текст */}
    {p.beats.map((b: any, i: number) => {
      if (t < b.start - 0.1 || t >= b.end + 0.05 || endK >= 1) return null;
      const fade = interpolate(t, [b.end - 0.25, b.end], [1, 0], clamp);
      const gs = groups(b.words);
      if (b.kind === 'hook') {
        return <div key={i} style={{position: 'absolute', left: M, width: 900, top: 250, fontFamily: 'Serif',
          fontSize: 112, lineHeight: .96, letterSpacing: '-.012em', opacity: fade}}>
          {gs.map((g, gi) => <div key={gi}>{g.map((w, wi) => <Word key={wi} w={w} t={t} serif mute={gi > 0} instant />)}</div>)}
        </div>;
      }
      const climax = b.kind === 'climax';
      const gi = Math.max(0, gs.findIndex((g, j) => t < (gs[j + 1]?.[0].t ?? 1e9) - 0.02));
      const lab = b.label ? interpolate(t, [b.arrive - 0.1, b.arrive + 0.45], [0, 1], out3) : 0;
      return <div key={i} style={{position: 'absolute', left: M, width: climax ? 860 : 820, bottom: 440, opacity: fade}}>
        {b.label && <div style={{fontFamily: 'Mono', fontSize: 22, letterSpacing: '.14em', textTransform: 'uppercase',
          display: 'flex', gap: 16, alignItems: 'center', marginBottom: 26, opacity: lab,
          transform: `translateX(${(1 - lab) * -14}px)`}}>
          <span style={{color: MUTE}}>{String(b.n).padStart(2, '0')}</span>
          <span style={{width: 34 * lab, height: 1, background: MUTE}} />
          <span>{b.label}</span>
        </div>}
        <div style={{fontFamily: climax ? 'Serif' : 'Grot', fontSize: climax ? 80 : 48, lineHeight: climax ? 1.02 : 1.16,
          letterSpacing: climax ? '-.01em' : '-.008em'}}>
          {climax ? gs.map((g, j) => <div key={j}>{g.map((w, wi) => <Word key={wi} w={w} t={t} serif />)}</div>)
            : gs[gi].map((w, wi) => <Word key={wi} w={w} t={t} size={48} />)}
        </div>
      </div>;
    })}

    {/* этикетка */}
    {endK > 0 && <AbsoluteFill style={{opacity: endK}}>
      <Bar text={p.rubric} />
      <div style={{position: 'absolute', left: M, top: p.labelTop, width: 936}}>
        <div style={{height: 1, background: 'rgba(242,238,230,.35)', marginBottom: 34,
          width: `${interpolate(t, [p.endStart + 1.0, p.endStart + 1.8], [0, 100], out3)}%`}} />
        {[<div key="a" style={{fontFamily: 'Serif', fontStyle: 'italic', fontSize: 86, lineHeight: 1}}>{p.title}</div>,
          <div key="b" style={{marginTop: 22, fontFamily: 'Grot', fontSize: 32, fontWeight: 450}}>{p.sub}</div>,
          <div key="c" style={{marginTop: 34, display: 'grid', gridTemplateColumns: '220px 1fr', rowGap: 12,
            fontFamily: 'Mono', fontSize: 21, letterSpacing: '.08em', textTransform: 'uppercase', color: MUTE}}>
            {(p.meta || []).map((m: string[]) => [<span key={m[0]}>{m[0]}</span>,
              <span key={m[0] + 'v'} style={{color: INK}}>{m[1]}</span>])}
          </div>].map((el, j) => {
          const k = interpolate(t, [p.endStart + 1.1 + j * .18, p.endStart + 1.7 + j * .18], [0, 1], out3);
          return <div key={j} style={{opacity: k, transform: `translateY(${(1 - k) * 14}px)`}}>{el}</div>;
        })}
      </div>
    </AbsoluteFill>}

    <Grain frame={frame} />
    {p.voice && <Audio src={p.voice} />}
    <Sfx list={p.sfx} fps={p.fps} />
  </AbsoluteFill>;
};

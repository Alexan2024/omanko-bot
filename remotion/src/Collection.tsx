import React from 'react';
import {AbsoluteFill, Img, interpolate, useCurrentFrame} from 'remotion';
import {Bar, Fonts, Grain, INK, M, MUTE, Rich, Sfx, clamp, easeSine, out3} from './common';

/* Подборка: титул на первой работе, затем работы по несколько секунд. Все работы подборки — в одном виде:
   mode = bleed — все на весь кадр (подборка вертикальных работ), mode = frame — все целиком, как на стене:
   низ картины на одной линии, подпись на одном месте. Без наездов на детали: работа видна вся.
   Смена — через короткое затемнение: старая уходит, потом появляется новая, кадры не накладываются. */

const OUT = 0.3, IN = 0.45;   // уход и появление работы, с

const Caption: React.FC<{it: any; i: number; total: number}> = ({it, i, total}) => <>
  <div style={{fontFamily: 'Mono', fontSize: 21, letterSpacing: '.14em', color: MUTE, marginBottom: 24,
    display: 'flex', gap: 18, alignItems: 'center'}}>
    <span style={{color: INK}}>{String(i + 1).padStart(2, '0')}</span>
    <span style={{width: 46, height: 1, background: MUTE}} />
    <span>{String(total).padStart(2, '0')}</span>
  </div>
  <div style={{fontFamily: 'Serif', fontSize: it.title.length > 42 ? 64 : 76, lineHeight: .98,
    letterSpacing: '-.01em', textWrap: 'balance' as any}}><Rich text={it.title} /></div>
  <div style={{marginTop: 22, fontFamily: 'Grot', fontSize: 30, fontWeight: 450, color: MUTE}}>
    {it.author}{it.year ? <>&nbsp;&nbsp;·&nbsp;&nbsp;{it.year}</> : null}</div>
</>;

export const Collection: React.FC<any> = (p) => {
  const frame = useCurrentFrame(); const t = frame / p.fps;
  const titleK = interpolate(t, [p.titleEnd - 0.4, p.titleEnd], [1, 0], clamp);
  const total = p.items.length;
  const bleed = p.mode === 'bleed';

  return <AbsoluteFill style={{background: '#0d0c0b', color: INK, overflow: 'hidden'}}>
    <Fonts />
    {p.items.map((it: any, i: number) => {
      const lt = t - it.start;
      if (lt < 0 || lt > it.dur) return null;
      const vis = Math.min(i === 0 ? 1 : easeSine(lt / IN), i === total - 1 ? 1 : easeSine((it.dur - lt) / OUT));
      const textIn = interpolate(lt, [0.35, 1.0], [0, 1], out3);
      const showText = i > 0 || t >= p.titleEnd - 0.1;
      const k = i === 0 ? interpolate(t, [p.titleEnd, p.titleEnd + 0.6], [0, 1], out3) : textIn;
      const lift = (1 - (i === 0 ? 1 : textIn)) * 14;
      // медленное приближение всей работы — как шаг к стене; работа остаётся видна целиком
      const z = 1 + (bleed ? 0.03 : 0.015) * easeSine(lt / it.dur);
      const caption = showText && <div style={{position: 'absolute', left: M, width: bleed ? 860 : 900,
        ...(bleed ? {bottom: 430} : {top: p.textTop}), opacity: k, transform: `translateY(${lift}px)`}}>
        <Caption it={it} i={i} total={total} /></div>;
      if (bleed) {
        return <AbsoluteFill key={i} style={{opacity: vis}}>
          <Img src={it.image} style={{position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover',
            transform: `scale(${z})`}} />
          <div style={{position: 'absolute', left: 0, right: 0, bottom: 0, height: 1000,
            background: 'linear-gradient(to top,rgba(8,7,6,.88),rgba(8,7,6,.6) 45%,rgba(8,7,6,0))'}} />
          <div style={{position: 'absolute', left: 0, right: 0, top: 0, height: 420,
            background: 'linear-gradient(to bottom,rgba(8,7,6,.7),rgba(8,7,6,0))'}} />
          {caption}
        </AbsoluteFill>;
      }
      const [x, y, w, h] = it.frame;
      return <AbsoluteFill key={i} style={{opacity: vis}}>
        <Img src={it.image} style={{position: 'absolute', left: x, top: y, width: w, height: h,
          transformOrigin: '50% 100%', transform: `scale(${z})`, boxShadow: '0 24px 70px rgba(0,0,0,.55)'}} />
        {caption}
      </AbsoluteFill>;
    })}

    {/* титул */}
    {titleK > 0 && <AbsoluteFill style={{opacity: titleK}}>
      <div style={{position: 'absolute', inset: 0, background: 'rgba(8,7,6,.55)'}} />
      <div style={{position: 'absolute', left: M, top: 620, width: 900, fontFamily: 'Serif', fontSize: 150, lineHeight: .9,
        letterSpacing: '-.02em', textWrap: 'balance' as any,
        opacity: interpolate(t, [0.1, 0.8], [0, 1], out3), transform: `translateY(${(1 - interpolate(t, [0.1, 0.8], [0, 1], out3)) * 20}px)`}}>
        <Rich text={p.title} /></div>
      {p.subtitle && <div style={{position: 'absolute', left: M, top: 1040, width: 700, fontFamily: 'Grot', fontSize: 38,
        fontWeight: 450, lineHeight: 1.25, color: MUTE, textWrap: 'balance' as any,
        opacity: interpolate(t, [0.6, 1.3], [0, 1], out3)}}>{p.subtitle}</div>}
    </AbsoluteFill>}

    <Bar text={t < p.titleEnd ? `Collection · ${total} works` : p.series} />
    <Grain frame={frame} />
    <Sfx list={p.sfx} fps={p.fps} />
  </AbsoluteFill>;
};

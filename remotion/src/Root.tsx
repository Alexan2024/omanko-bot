import React from 'react';
import {Composition} from 'remotion';
import {Story} from './Story';
import {Collection} from './Collection';

// Длительность приходит из props, которые готовит бот (app/reelplan.py).
const meta = ({props}: {props: any}) => ({durationInFrames: Math.max(1, Math.round(props.duration || 30))});

export const Root: React.FC = () => (
  <>
    <Composition id="Story" component={Story as any} durationInFrames={300} fps={30} width={1080} height={1920}
      defaultProps={{} as any} calculateMetadata={meta as any} />
    <Composition id="Collection" component={Collection as any} durationInFrames={300} fps={30} width={1080} height={1920}
      defaultProps={{} as any} calculateMetadata={meta as any} />
  </>
);

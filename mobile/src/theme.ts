// Same palette as the web screens and the pitch deck.
export const colors = {
  bg: '#F3F5F1',
  card: '#FBFCF9',
  line: '#D3DCD6',
  ink: '#10201B',
  soft: '#46554F',
  muted: '#5E6B66',
  green: '#17765A',
  greenSoft: '#D5EDE2',
  amber: '#8F5E0E',
  amberSoft: '#F2E8D5',
  red: '#A93626',
  redSoft: '#F6E3DF',
  blue: '#3B4A9E',
  blueSoft: '#E5E8F4',
  chip: '#E4EAE6',
  white: '#FFFFFF',
};

export const rupees = (paise: number) =>
  '₹' + (paise / 100).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

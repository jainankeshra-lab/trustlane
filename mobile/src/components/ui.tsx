import type { ReactNode } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View, type ViewStyle } from 'react-native';

import { colors } from '@/theme';

export function Card({ children, style }: { children: ReactNode; style?: ViewStyle }) {
  return <View style={[styles.card, style]}>{children}</View>;
}

export function Eyebrow({ children, color = colors.green }: { children: ReactNode; color?: string }) {
  return <Text style={[styles.eyebrow, { color }]}>{children}</Text>;
}

export function Title({ children }: { children: ReactNode }) {
  return <Text style={styles.title} accessibilityRole="header">{children}</Text>;
}

export function Muted({ children, small }: { children: ReactNode; small?: boolean }) {
  return <Text style={[styles.muted, small && styles.small]}>{children}</Text>;
}

type ButtonProps = {
  label: string;
  onPress: () => void;
  kind?: 'primary' | 'plain' | 'ghost';
  disabled?: boolean;
  busy?: boolean;
  small?: boolean;
  accessibilityLabel?: string;  // when the visible label alone is ambiguous ("Put back" -> which item)
};

export function Button({ label, onPress, kind = 'plain', disabled, busy, small, accessibilityLabel }: ButtonProps) {
  const primary = kind === 'primary';
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? label}
      accessibilityState={{ disabled: !!disabled, busy: !!busy }}
      disabled={disabled || busy}
      onPress={onPress}
      style={({ pressed }) => [
        styles.button,
        primary && styles.primary,
        kind === 'ghost' && styles.ghost,
        small && styles.buttonSmall,
        (disabled || busy) && styles.disabled,
        pressed && { opacity: 0.8 },
      ]}>
      {busy ? (
        <ActivityIndicator color={primary ? colors.white : colors.ink} />
      ) : (
        <Text style={[styles.buttonText, primary && { color: colors.white }, small && styles.small]}>{label}</Text>
      )}
    </Pressable>
  );
}

const CHIP: Record<string, [string, string]> = {
  verified: [colors.greenSoft, colors.green],
  green: [colors.greenSoft, colors.green],
  flagged: [colors.amberSoft, colors.amber],
  warn: [colors.amberSoft, colors.amber],
  red: [colors.redSoft, colors.red],
  removed: [colors.chip, colors.muted],
  refunded: [colors.chip, colors.muted],
  info: [colors.blueSoft, colors.blue],
};

export function Chip({ label, kind = 'info' }: { label: string; kind?: string }) {
  const [bg, fg] = CHIP[kind] ?? [colors.chip, colors.ink];
  return (
    <View style={[styles.chip, { backgroundColor: bg }]} accessibilityRole="text">
      <Text style={[styles.chipText, { color: fg }]}>{label}</Text>
    </View>
  );
}

export function Banner({ children, kind = 'info' }: { children: ReactNode; kind?: 'info' | 'warn' }) {
  return (
    <View style={[styles.banner, { backgroundColor: kind === 'warn' ? colors.amberSoft : colors.blueSoft }]}
      accessibilityRole={kind === 'warn' ? 'alert' : undefined}>
      <Text style={styles.bannerText}>{children}</Text>
    </View>
  );
}

export const styles = StyleSheet.create({
  card: { backgroundColor: colors.card, borderColor: colors.line, borderWidth: 1, borderRadius: 14, padding: 16, gap: 10 },
  eyebrow: { fontSize: 12, letterSpacing: 1, textTransform: 'uppercase', fontWeight: '600' },
  title: { fontSize: 24, fontWeight: '700', color: colors.ink },
  muted: { color: colors.muted, fontSize: 15 },
  small: { fontSize: 13 },
  button: {
    minHeight: 48, borderRadius: 12, borderWidth: 1, borderColor: colors.line, backgroundColor: colors.card,
    alignItems: 'center', justifyContent: 'center', paddingHorizontal: 16,
  },
  buttonSmall: { minHeight: 44, paddingHorizontal: 12 },  // 44 pt: the smallest comfortable touch target
  primary: { backgroundColor: colors.green, borderColor: colors.green },
  ghost: { backgroundColor: 'transparent' },
  disabled: { opacity: 0.5 },
  buttonText: { fontSize: 16, fontWeight: '600', color: colors.ink },
  chip: { borderRadius: 999, paddingHorizontal: 9, paddingVertical: 2, alignSelf: 'flex-start' },
  chipText: { fontSize: 12, fontWeight: '700' },
  banner: { borderRadius: 12, padding: 12 },
  bannerText: { color: colors.ink, fontSize: 14 },
});

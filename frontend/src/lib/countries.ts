export const COUNTRY_OPTIONS = [
  ["RO", "Romania"],
  ["US", "United States"],
  ["GB", "United Kingdom"],
  ["CA", "Canada"],
  ["AU", "Australia"],
  ["NZ", "New Zealand"],
  ["DE", "Germany"],
  ["FR", "France"],
  ["ES", "Spain"],
  ["IT", "Italy"],
  ["NL", "Netherlands"],
  ["IE", "Ireland"],
  ["JP", "Japan"],
  ["KR", "South Korea"],
  ["IN", "India"],
  ["BR", "Brazil"],
] as const;

export function countryName(code: string): string {
  const option = COUNTRY_OPTIONS.find(([value]) => value === code);
  return option ? option[1] : code;
}

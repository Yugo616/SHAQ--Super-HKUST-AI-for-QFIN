function decide(input) {
  if (!input.synthesis) throw new Error("Validated synthesis is required");
  return input.synthesis;
}

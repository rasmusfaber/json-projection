/// Whether a byte belongs to the scalar and whether the scalar has ended.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum Advance {
    Consumed,
    Complete,
    Delimiter,
}

/// Incrementally validates a scalar without retaining or decoding its contents.
///
/// Like jiter's range decoder, this validates escapes but leaves raw UTF-8
/// validation to the caller. Integer prefixes include the optional minus sign
/// in jiter's 4,300-byte limit; fraction and exponent tails are unlimited.
#[derive(Debug)]
pub(super) struct Scalar {
    state: State,
}

#[derive(Clone, Copy, Debug)]
enum State {
    String,
    Escape,
    Unicode {
        value: u16,
        remaining: u8,
        low_surrogate: bool,
    },
    SurrogateBackslash,
    SurrogateU,
    Literal {
        expected: &'static [u8],
        next: usize,
    },
    Minus,
    Zero,
    Integer {
        prefix_len: u16,
    },
    FractionStart,
    Fraction,
    ExponentStart,
    ExponentSign,
    Exponent,
    Done,
}

impl Scalar {
    /// Starts a scalar, consuming its first byte.
    pub(super) fn start(first: u8) -> Result<Self, &'static str> {
        let state = match first {
            b'"' => State::String,
            b't' => State::Literal {
                expected: b"true",
                next: 1,
            },
            b'f' => State::Literal {
                expected: b"false",
                next: 1,
            },
            b'n' => State::Literal {
                expected: b"null",
                next: 1,
            },
            b'N' => State::Literal {
                expected: b"NaN",
                next: 1,
            },
            b'I' => State::Literal {
                expected: b"Infinity",
                next: 1,
            },
            b'-' => State::Minus,
            b'0' => State::Zero,
            b'1'..=b'9' => State::Integer { prefix_len: 1 },
            _ => return Err("expected a JSON scalar"),
        };
        Ok(Self { state })
    }

    /// Consumes a byte, or returns `Delimiter` so the caller can reprocess it.
    pub(super) fn push(&mut self, byte: u8) -> Result<Advance, &'static str> {
        self.state = match self.state {
            State::String => match byte {
                b'"' => {
                    self.state = State::Done;
                    return Ok(Advance::Complete);
                }
                b'\\' => State::Escape,
                0..=0x1f => return Err("control character in string"),
                _ => State::String,
            },
            State::Escape => match byte {
                b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't' => State::String,
                b'u' => State::Unicode {
                    value: 0,
                    remaining: 4,
                    low_surrogate: false,
                },
                _ => return Err("invalid string escape"),
            },
            State::Unicode {
                value,
                remaining,
                low_surrogate,
            } => {
                let hex = match byte {
                    b'0'..=b'9' => byte - b'0',
                    b'a'..=b'f' => byte - b'a' + 10,
                    b'A'..=b'F' => byte - b'A' + 10,
                    _ => return Err("invalid Unicode escape"),
                };
                let value = (value << 4) | u16::from(hex);
                if remaining > 1 {
                    State::Unicode {
                        value,
                        remaining: remaining - 1,
                        low_surrogate,
                    }
                } else if low_surrogate {
                    if !(0xdc00..=0xdfff).contains(&value) {
                        return Err("expected a low surrogate");
                    }
                    State::String
                } else {
                    match value {
                        0xd800..=0xdbff => State::SurrogateBackslash,
                        0xdc00..=0xdfff => return Err("unpaired low surrogate"),
                        _ => State::String,
                    }
                }
            }
            State::SurrogateBackslash => match byte {
                b'\\' => State::SurrogateU,
                _ => return Err("expected a low surrogate escape"),
            },
            State::SurrogateU => match byte {
                b'u' => State::Unicode {
                    value: 0,
                    remaining: 4,
                    low_surrogate: true,
                },
                _ => return Err("expected a low surrogate escape"),
            },
            State::Literal { expected, next } => {
                if byte != expected[next] {
                    return Err("invalid JSON literal");
                }
                if next + 1 == expected.len() {
                    self.state = State::Done;
                    return Ok(Advance::Complete);
                }
                State::Literal {
                    expected,
                    next: next + 1,
                }
            }
            State::Minus => match byte {
                b'0' => State::Zero,
                b'1'..=b'9' => State::Integer { prefix_len: 2 },
                b'I' => State::Literal {
                    expected: b"Infinity",
                    next: 1,
                },
                _ => return Err("invalid number"),
            },
            State::Zero => match byte {
                b'0'..=b'9' => return Err("leading zero in number"),
                b'.' => State::FractionStart,
                b'e' | b'E' => State::ExponentStart,
                _ => return Ok(self.delimiter()),
            },
            State::Integer { prefix_len } => match byte {
                b'0'..=b'9' => {
                    if prefix_len == 4300 {
                        return Err("number integer prefix exceeds 4300 bytes");
                    }
                    State::Integer {
                        prefix_len: prefix_len + 1,
                    }
                }
                b'.' => State::FractionStart,
                b'e' | b'E' => State::ExponentStart,
                _ => return Ok(self.delimiter()),
            },
            State::FractionStart => match byte {
                b'0'..=b'9' => State::Fraction,
                _ => return Err("expected a fraction digit"),
            },
            State::Fraction => match byte {
                b'0'..=b'9' => State::Fraction,
                b'e' | b'E' => State::ExponentStart,
                _ => return Ok(self.delimiter()),
            },
            State::ExponentStart => match byte {
                b'+' | b'-' => State::ExponentSign,
                b'0'..=b'9' => State::Exponent,
                _ => return Err("expected an exponent digit"),
            },
            State::ExponentSign => match byte {
                b'0'..=b'9' => State::Exponent,
                _ => return Err("expected an exponent digit"),
            },
            State::Exponent => match byte {
                b'0'..=b'9' => State::Exponent,
                _ => return Ok(self.delimiter()),
            },
            State::Done => return Err("scalar is already complete"),
        };
        Ok(Advance::Consumed)
    }

    fn delimiter(&mut self) -> Advance {
        self.state = State::Done;
        Advance::Delimiter
    }

    /// Confirms the scalar could end at EOF without preventing later input.
    pub(super) fn finish(&self) -> Result<(), &'static str> {
        match self.state {
            State::Zero
            | State::Integer { .. }
            | State::Fraction
            | State::Exponent
            | State::Done => Ok(()),
            _ => Err("incomplete JSON scalar"),
        }
    }

    /// Returns a prefix that can be consumed without changing lexer state.
    pub(super) fn span(&self, bytes: &[u8]) -> usize {
        match self.state {
            State::String => bytes
                .iter()
                .position(|&byte| byte < 0x20 || matches!(byte, b'"' | b'\\'))
                .unwrap_or(bytes.len()),
            State::Fraction | State::Exponent => bytes
                .iter()
                .position(|byte| !byte.is_ascii_digit())
                .unwrap_or(bytes.len()),
            _ => 0,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{Advance, Scalar};

    fn validate(bytes: &[u8]) -> Result<(), &'static str> {
        let first = *bytes.first().ok_or("empty input")?;
        let mut scalar = Scalar::start(first)?;
        for (index, &byte) in bytes.iter().enumerate().skip(1) {
            match scalar.push(byte)? {
                Advance::Consumed => {}
                Advance::Complete if index + 1 == bytes.len() => return Ok(()),
                Advance::Complete | Advance::Delimiter => return Err("trailing input"),
            }
        }
        scalar.finish()
    }

    fn state_after(bytes: &[u8]) -> Scalar {
        let mut scalar = Scalar::start(bytes[0]).unwrap();
        for &byte in &bytes[1..] {
            assert_eq!(scalar.push(byte).unwrap(), Advance::Consumed);
        }
        scalar
    }

    #[test]
    fn accepts_strings_literals_and_numbers() {
        for value in [
            "\"\"",
            "\"hello 世界\"",
            r#""\"\\\/\b\f\n\r\t""#,
            r#""\u0000\u0020\uD7FF\uE000\uFFFF""#,
            r#""\uD800\uDC00\uDBff\udFFF""#,
            "true",
            "false",
            "null",
            "NaN",
            "Infinity",
            "-Infinity",
            "0",
            "-0",
            "123456789012345678901234567890",
            "-123456789012345678901234567890",
            "0.0",
            "-0.01",
            "123.45",
            "0e0",
            "1E+2",
            "1e-2",
            "-1.2E+34",
        ] {
            assert_eq!(validate(value.as_bytes()), Ok(()), "{value:?}");
        }
    }

    #[test]
    fn rejects_malformed_and_incomplete_tokens() {
        for value in [
            "",
            " ",
            "+1",
            ".5",
            "00",
            "01",
            "-00",
            "-01",
            "--1",
            "-NaN",
            "nan",
            "inf",
            "TRUE",
            "nulL",
            "truf",
            "NaX",
            "Infinitx",
            "-Infinitx",
            "1.",
            "1.e2",
            "1e",
            "1e+",
            "1e-",
            "1e++2",
            "1e+-2",
            "1e.2",
            "-",
            "t",
            "tr",
            "tru",
            "fals",
            "nul",
            "Na",
            "Infinit",
            "-Infinit",
            "\"",
            "\"abc",
            "\"\\",
            "\"\\u",
            "\"\\u123",
            "\"\\x\"",
            "\"\\u123x\"",
            "\"\n\"",
            "\"\u{0}\"",
            "\"\u{1f}\"",
            r#""\uDC00""#,
            r#""\uDFFF""#,
            r#""\uD800""#,
            r#""\uD800x""#,
            r#""\uD800\x""#,
            r#""\uD800\u0041""#,
            r#""\uD800\uD800""#,
            r#""\uD800\u""#,
            r#""\uD800\uDC0""#,
        ] {
            assert!(validate(value.as_bytes()).is_err(), "{value:?}");
        }
    }

    #[test]
    fn incomplete_prefixes_can_continue_across_chunks() {
        for token in [
            r#""abc\n\u0041\uD800\uDC00z""#,
            "true",
            "false",
            "null",
            "NaN",
            "Infinity",
            "-Infinity",
            "1.5",
            "1e+2",
            "-123.456e-789",
        ] {
            let bytes = token.as_bytes();
            for split in 1..bytes.len() {
                let mut scalar = state_after(&bytes[..split]);
                let is_number =
                    matches!(bytes[0], b'-' | b'0'..=b'9') && !token.ends_with("Infinity");
                if !is_number {
                    assert!(scalar.finish().is_err(), "{token:?} at {split}");
                }
                for (index, &byte) in bytes.iter().enumerate().skip(split) {
                    let expected = if index + 1 == bytes.len() && !is_number {
                        Advance::Complete
                    } else {
                        Advance::Consumed
                    };
                    assert_eq!(scalar.push(byte).unwrap(), expected, "{token:?} at {split}");
                }
                assert_eq!(scalar.finish(), Ok(()));
            }
        }
    }

    #[test]
    fn numbers_wait_for_delimiters_or_eof() {
        for prefix in ["0", "-0", "1", "-12", "1.5", "1e+2"] {
            for delimiter in [b' ', b'\t', b'\r', b'\n', b',', b']', b'}', b'x'] {
                let mut scalar = state_after(prefix.as_bytes());
                assert_eq!(scalar.finish(), Ok(()));
                assert_eq!(scalar.push(delimiter), Ok(Advance::Delimiter));
            }
        }
        for (prefix, continuation) in [("1", ".5"), ("1", "e+2"), ("0", ".5")] {
            let mut scalar = state_after(prefix.as_bytes());
            assert_eq!(scalar.finish(), Ok(()));
            for byte in continuation.bytes() {
                assert_eq!(scalar.push(byte), Ok(Advance::Consumed));
            }
            assert_eq!(scalar.finish(), Ok(()));
        }
    }

    #[test]
    fn integer_prefix_cap_includes_minus_sign() {
        for negative in [false, true] {
            for prefix_len in [4299, 4300, 4301] {
                let prefix = format!(
                    "{}{}",
                    if negative { "-" } else { "" },
                    "1".repeat(prefix_len - usize::from(negative))
                );
                for suffix in ["", ".5", "e2", ".5e2"] {
                    let value = format!("{prefix}{suffix}");
                    assert_eq!(
                        validate(value.as_bytes()).is_ok(),
                        prefix_len <= 4300,
                        "negative={negative}, prefix_len={prefix_len}, suffix={suffix}"
                    );
                }
            }
        }
    }

    #[test]
    fn fraction_and_exponent_tails_have_no_length_limit() {
        assert!(std::mem::size_of::<Scalar>() <= 64);
        for prefix in ["1.0", "-1.0", "1e0", "1e+0", "1e-0"] {
            let mut scalar = state_after(prefix.as_bytes());
            for _ in 0..1_000_000 {
                assert_eq!(scalar.push(b'9'), Ok(Advance::Consumed));
            }
            assert_eq!(scalar.finish(), Ok(()));
            assert_eq!(scalar.push(b','), Ok(Advance::Delimiter));
        }
    }

    #[test]
    fn string_spans_stop_before_special_bytes() {
        let mut scalar = Scalar::start(b'"').unwrap();
        for bytes in [
            b"plain string".as_slice(),
            b"space and utf8 \xc3\xa9",
            b"raw invalid utf8 \xff",
        ] {
            assert_eq!(scalar.span(bytes), bytes.len());
        }
        assert_eq!(scalar.span(b""), 0);
        for special in [b'"', b'\\', 0, 1, 0x1f] {
            assert_eq!(scalar.span(&[b'a', special, b'b']), 1);
            assert_eq!(scalar.span(&[special, b'b']), 0);
        }
        assert_eq!(scalar.push(b'\\'), Ok(Advance::Consumed));
        assert_eq!(scalar.span(b"abc123"), 0);
        assert_eq!(scalar.push(b'n'), Ok(Advance::Consumed));
        assert_eq!(scalar.span(b"abc123"), 6);
        assert_eq!(scalar.push(b'"'), Ok(Advance::Complete));
    }

    #[test]
    fn digit_spans_only_apply_after_required_digits() {
        for prefix in ["1.0", "1e0", "1e+0", "1e-0", "-1.0"] {
            let mut scalar = state_after(prefix.as_bytes());
            assert_eq!(scalar.span(b""), 0);
            assert_eq!(scalar.span(b"1234567890"), 10);
            assert_eq!(scalar.span(b"123e+4"), 3);
            assert_eq!(scalar.span(b"123.4"), 3);
            assert_eq!(scalar.span(b"123,"), 3);
            assert_eq!(scalar.span(b"e+4"), 0);
            assert_eq!(scalar.push(b','), Ok(Advance::Delimiter));
        }
        for prefix in ["0", "1", "-", "-1", "1.", "1e", "1e+", "1e-"] {
            assert_eq!(state_after(prefix.as_bytes()).span(b"1234567890"), 0);
        }
        for prefix in [r#""\"#, r#""\u1"#, r#""\uD800"#, r#""\uD800\"#, "t", "I"] {
            assert_eq!(state_after(prefix.as_bytes()).span(b"1234567890"), 0);
        }
    }

    #[test]
    fn spans_preserve_state_when_skipping_large_payloads() {
        let digits = vec![b'9'; 1_000_000];
        let mut scalar = state_after(b"1.0");
        assert_eq!(scalar.span(&digits), digits.len());
        assert_eq!(scalar.push(b'e'), Ok(Advance::Consumed));
        assert_eq!(scalar.push(b'-'), Ok(Advance::Consumed));
        assert_eq!(scalar.push(b'1'), Ok(Advance::Consumed));
        assert_eq!(scalar.span(&digits), digits.len());
        assert_eq!(scalar.finish(), Ok(()));

        let mut string = Scalar::start(b'"').unwrap();
        assert_eq!(string.span(&digits), digits.len());
        assert_eq!(string.push(b'"'), Ok(Advance::Complete));
        assert_eq!(string.span(&digits), 0);
    }

    #[test]
    fn raw_utf8_is_not_validated() {
        assert_eq!(validate(&[b'"', 0xff, 0xc0, 0x80, b'"']), Ok(()));
    }
}

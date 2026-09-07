mod lexer;
mod spec;

use std::sync::Arc;

use lexer::{Advance, Scalar};
use spec::Node;

pub(crate) use spec::StreamPlan;

const MAX_DEPTH: usize = 200;

#[derive(Debug)]
pub(crate) enum Error {
    Parse { offset: usize, message: String },
    Closed,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Status {
    Active,
    Finished,
    Failed,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Action {
    Copy,
    Discard,
    Project(usize),
}

#[derive(Clone, Copy)]
enum Expect {
    ObjectFirst,
    ObjectKey,
    Colon,
    ObjectValue,
    ObjectComma,
    ArrayFirst,
    ArrayValue,
    ArrayComma,
}

struct Frame {
    action: Action,
    expect: Expect,
    next: Action,
    emitted: bool,
    object: bool,
}

#[derive(Clone, Copy)]
enum Destination {
    Output,
    Discard,
    LookupKey,
}

struct Token {
    scalar: Scalar,
    destination: Destination,
    key: bool,
    start: usize,
}

/// One document's scanner. Input slices are borrowed only during `feed`.
pub(crate) struct Engine {
    plan: Arc<StreamPlan>,
    frames: Vec<Frame>,
    token: Option<Token>,
    key: Vec<u8>,
    output: Vec<u8>,
    offset: usize,
    started: bool,
    status: Status,
}

impl Engine {
    pub(crate) fn new(plan: Arc<StreamPlan>) -> Self {
        Self {
            plan,
            frames: Vec::new(),
            token: None,
            key: Vec::new(),
            output: Vec::new(),
            offset: 0,
            started: false,
            status: Status::Active,
        }
    }

    pub(crate) fn feed(&mut self, chunk: &[u8]) -> Result<(), Error> {
        if self.status != Status::Active {
            return Err(Error::Closed);
        }
        if let Err(error) = self.consume(chunk) {
            self.fail();
            return Err(error);
        }
        Ok(())
    }

    pub(crate) fn finish(&mut self) -> Result<Vec<u8>, Error> {
        if self.status != Status::Active {
            return Err(Error::Closed);
        }
        let result = if let Some(token) = &self.token {
            token.scalar.finish().map_err(|message| self.error(message))
        } else {
            Ok(())
        };
        let result = result.and_then(|()| {
            if !self.started || !self.frames.is_empty() {
                Err(self.error("incomplete JSON object"))
            } else {
                Ok(())
            }
        });
        if let Err(error) = result {
            self.fail();
            return Err(error);
        }
        self.status = Status::Finished;
        self.frames = Vec::new();
        self.key = Vec::new();
        Ok(std::mem::take(&mut self.output))
    }

    fn error(&self, message: &str) -> Error {
        Error::Parse {
            offset: self.offset,
            message: message.to_owned(),
        }
    }

    fn fail(&mut self) {
        self.status = Status::Failed;
        self.frames = Vec::new();
        self.token = None;
        self.key = Vec::new();
        self.output = Vec::new();
    }

    fn write(&mut self, destination: Destination, bytes: &[u8]) {
        match destination {
            Destination::Output => self.output.extend_from_slice(bytes),
            Destination::LookupKey => self.key.extend_from_slice(bytes),
            Destination::Discard => (),
        }
    }

    fn consume(&mut self, chunk: &[u8]) -> Result<(), Error> {
        let mut index = 0;
        while index < chunk.len() {
            if let Some(token) = &self.token {
                let span = token.scalar.span(&chunk[index..]);
                let destination = token.destination;
                if span > 0 {
                    self.write(destination, &chunk[index..index + span]);
                    index += span;
                    self.offset += span;
                    continue;
                }
                let advance = self
                    .token
                    .as_mut()
                    .unwrap()
                    .scalar
                    .push(chunk[index])
                    .map_err(|message| self.error(message))?;
                match advance {
                    Advance::Consumed | Advance::Complete => {
                        self.write(destination, &chunk[index..index + 1]);
                        index += 1;
                        self.offset += 1;
                        if matches!(advance, Advance::Complete) {
                            self.complete_token()?;
                        }
                    }
                    Advance::Delimiter => self.complete_token()?,
                }
            } else {
                self.structural(chunk[index])?;
                index += 1;
                self.offset += 1;
            }
        }
        Ok(())
    }

    fn structural(&mut self, byte: u8) -> Result<(), Error> {
        let whitespace = matches!(byte, b' ' | b'\t' | b'\r' | b'\n');
        if !self.started {
            if whitespace {
                return Ok(());
            }
            if byte != b'{' {
                return Err(self.error("the JSON root is not an object"));
            }
            self.started = true;
            return self.begin_value(byte, Action::Project(0));
        }
        let Some(frame) = self.frames.last() else {
            return if whitespace {
                Ok(())
            } else {
                Err(self.error("trailing input"))
            };
        };
        if whitespace {
            if frame.action == Action::Copy
                || (matches!(frame.action, Action::Project(_))
                    && matches!(frame.expect, Expect::Colon | Expect::ObjectValue)
                    && frame.next != Action::Discard)
            {
                self.output.push(byte);
            }
            return Ok(());
        }
        match frame.expect {
            Expect::ObjectFirst if byte == b'}' => self.close(byte),
            Expect::ObjectFirst | Expect::ObjectKey => {
                if byte != b'"' {
                    return Err(self.error("expected an object key"));
                }
                let destination = match frame.action {
                    Action::Copy => Destination::Output,
                    Action::Discard => Destination::Discard,
                    Action::Project(_) => Destination::LookupKey,
                };
                self.begin_scalar(byte, destination, true)
            }
            Expect::Colon => {
                if byte != b':' {
                    return Err(self.error("expected ':' after object key"));
                }
                if frame.next != Action::Discard {
                    self.output.push(byte);
                }
                self.frames.last_mut().unwrap().expect = Expect::ObjectValue;
                Ok(())
            }
            Expect::ObjectValue => self.begin_value(byte, frame.next),
            Expect::ObjectComma => match byte {
                b'}' => self.close(byte),
                b',' => {
                    if frame.action == Action::Copy {
                        self.output.push(byte);
                    }
                    self.frames.last_mut().unwrap().expect = Expect::ObjectKey;
                    Ok(())
                }
                _ => Err(self.error("expected ',' or '}'")),
            },
            Expect::ArrayFirst if byte == b']' => self.close(byte),
            Expect::ArrayFirst | Expect::ArrayValue => {
                let action = match frame.action {
                    Action::Project(node) => {
                        let Node::Array(child) = self.plan.node(node) else {
                            unreachable!()
                        };
                        if frame.emitted {
                            self.output.push(b',');
                        }
                        let child = *child;
                        self.frames.last_mut().unwrap().emitted = true;
                        Action::Project(child)
                    }
                    action => action,
                };
                self.begin_value(byte, action)
            }
            Expect::ArrayComma => match byte {
                b']' => self.close(byte),
                b',' => {
                    if frame.action == Action::Copy {
                        self.output.push(byte);
                    }
                    self.frames.last_mut().unwrap().expect = Expect::ArrayValue;
                    Ok(())
                }
                _ => Err(self.error("expected ',' or ']'")),
            },
        }
    }

    fn begin_value(&mut self, byte: u8, mut action: Action) -> Result<(), Error> {
        if let Action::Project(node) = action {
            if !matches!(
                (self.plan.node(node), byte),
                (Node::Object(_), b'{') | (Node::Array(_), b'[')
            ) {
                action = Action::Copy;
            }
        }
        if matches!(byte, b'{' | b'[') {
            if self.frames.len() >= MAX_DEPTH {
                return Err(self.error("nesting exceeds 200 containers"));
            }
            if action != Action::Discard {
                self.output.push(byte);
            }
            let object = byte == b'{';
            self.frames.push(Frame {
                action,
                object,
                emitted: false,
                next: Action::Discard,
                expect: if object {
                    Expect::ObjectFirst
                } else {
                    Expect::ArrayFirst
                },
            });
            Ok(())
        } else {
            self.begin_scalar(
                byte,
                if action == Action::Discard {
                    Destination::Discard
                } else {
                    Destination::Output
                },
                false,
            )
        }
    }

    fn begin_scalar(&mut self, byte: u8, destination: Destination, key: bool) -> Result<(), Error> {
        let scalar = Scalar::start(byte).map_err(|message| self.error(message))?;
        self.token = Some(Token {
            scalar,
            destination,
            key,
            start: self.offset,
        });
        self.write(destination, &[byte]);
        Ok(())
    }

    fn complete_token(&mut self) -> Result<(), Error> {
        let token = self.token.take().unwrap();
        if !token.key {
            self.complete_value();
            return Ok(());
        }
        let frame = self.frames.last_mut().unwrap();
        frame.next = match frame.action {
            Action::Project(node) => {
                let mut parser = jiter::Jiter::new(&self.key);
                let key = match parser.next_str() {
                    Ok(key) => key,
                    Err(error) => {
                        return Err(Error::Parse {
                            offset: token.start + error.index,
                            message: "invalid JSON object key".to_owned(),
                        })
                    }
                };
                let Node::Object(fields) = self.plan.node(node) else {
                    unreachable!()
                };
                match fields.get(key) {
                    Some(child) => {
                        if frame.emitted {
                            self.output.push(b',');
                        }
                        frame.emitted = true;
                        self.output.extend_from_slice(&self.key);
                        Action::Project(*child)
                    }
                    None => Action::Discard,
                }
            }
            action => action,
        };
        self.key.clear();
        frame.expect = Expect::Colon;
        Ok(())
    }

    fn close(&mut self, byte: u8) -> Result<(), Error> {
        let frame = self.frames.pop().unwrap();
        if frame.action != Action::Discard {
            self.output.push(byte);
        }
        self.complete_value();
        Ok(())
    }

    fn complete_value(&mut self) {
        if let Some(frame) = self.frames.last_mut() {
            frame.expect = if frame.object {
                Expect::ObjectComma
            } else {
                Expect::ArrayComma
            };
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::sync::Arc;

    use super::*;
    use crate::spec::Spec;
    use crate::walk::project_bytes;

    fn selection() -> Spec {
        Spec::Object(HashMap::from([
            ("id".into(), Spec::Keep),
            (
                "items".into(),
                Spec::Array(Box::new(Spec::Object(HashMap::from([(
                    "id".into(),
                    Spec::Keep,
                )])))),
            ),
        ]))
    }

    #[test]
    fn every_boundary_matches_the_whole_buffer_walk() {
        let cases: &[&[u8]] = &[
            b"{}",
            b" { \n\t } \r\n",
            br#"{"id":1,"drop":[false,{"x":"\uD83D\uDE00"}],"id":2}"#,
            br#"{"items":[{"id":1,"drop":[1,2]},{"id":3}],"id":1.2e+03}"#,
            b"{ \n \"\\u0069d\" \t : \n [ 1 , 2 ] , \"drop\": true }",
            br#"{"items":{"wrong":"shape"},"id":NaN}"#,
            br#"{"id":-Infinity,"items":[null,4,[],{},[{"id":2}]]}"#,
        ];
        let spec = selection();
        let plan = Arc::new(StreamPlan::new(&spec));
        for raw in cases {
            let expected = project_bytes(raw, &spec).unwrap();
            for split in 0..=raw.len() {
                let mut engine = Engine::new(plan.clone());
                engine.feed(&raw[..split]).unwrap();
                engine.feed(b"").unwrap();
                engine.feed(&raw[split..]).unwrap();
                assert_eq!(engine.finish().unwrap(), expected, "split {split}: {raw:?}");
            }
        }
    }

    #[test]
    fn malformed_discarded_data_and_trailing_input_are_errors() {
        let cases: &[&[u8]] = &[
            b"",
            b"[]",
            b"null",
            b"{}{}",
            b"{\"drop\": [1,]}",
            b"{\"drop\": {\"x\" 1}}",
            b"{\"drop\": 01}",
            b"{\"drop\": truex}",
            b"{\"drop\": 1e+}",
            br#"{"drop":"\uDC00"}"#,
            br#"{"drop":"\uD800\u0041"}"#,
            b"{\"drop\": [1}",
            b"{\"drop\": 1,}",
            b"{\"drop\":",
        ];
        let plan = Arc::new(StreamPlan::new(&selection()));
        for raw in cases {
            for split in 0..=raw.len() {
                let mut engine = Engine::new(plan.clone());
                let result = engine
                    .feed(&raw[..split])
                    .and_then(|()| engine.feed(&raw[split..]))
                    .and_then(|()| engine.finish().map(drop));
                assert!(matches!(result, Err(Error::Parse { .. })), "{raw:?}");
                assert!(matches!(engine.feed(b"{}"), Err(Error::Closed)));
                assert!(matches!(engine.finish(), Err(Error::Closed)));
            }
        }
    }

    #[test]
    fn nesting_limit_counts_root_and_discarded_containers() {
        for depth in [199, 200, 201] {
            let mut raw = b"{\"drop\":".to_vec();
            raw.extend(vec![b'['; depth - 1]);
            raw.push(b'0');
            raw.extend(vec![b']'; depth - 1]);
            raw.push(b'}');
            let mut engine = Engine::new(Arc::new(StreamPlan::new(&selection())));
            let result = engine.feed(&raw).and_then(|()| engine.finish());
            assert_eq!(result.is_ok(), depth <= 200);
        }
    }
}

use jiter::{Jiter, JiterError, Peek};

use crate::spec::Spec;

#[derive(Debug)]
pub enum WalkError {
    /// The root value is not an object; the caller passes the input through.
    NotAnObject,
    /// A JSON syntax, depth or number error; the caller passes the input through or raises.
    Json(JiterError),
}

impl From<JiterError> for WalkError {
    fn from(e: JiterError) -> Self {
        WalkError::Json(e)
    }
}

/// Project `data` through `spec`, copying kept members as raw bytes and skipping everything else.
pub fn project_bytes(data: &[u8], spec: &Spec) -> Result<Vec<u8>, WalkError> {
    let mut jiter = Jiter::new(data).with_allow_inf_nan();
    if jiter.peek()? != Peek::Object {
        return Err(WalkError::NotAnObject);
    }
    let mut out = Vec::with_capacity(data.len() / 8);
    project_value(&mut jiter, data, Peek::Object, spec, &mut out)?;
    jiter.finish()?;
    Ok(out)
}

/// `i` sits at `ws* ('{' | ',') ws* '"'` (already validated by jiter); return the index of the `"`.
fn key_start(data: &[u8], mut i: usize) -> usize {
    let ws = |b: u8| matches!(b, b' ' | b'\t' | b'\n' | b'\r');
    while ws(data[i]) {
        i += 1;
    }
    i += 1; // the '{' or ','
    while ws(data[i]) {
        i += 1;
    }
    i
}

/// Cursor at the first byte of a value (`peek()` consumed the whitespace). Skip it and copy the raw bytes.
fn copy_value(j: &mut Jiter, data: &[u8], peek: Peek, out: &mut Vec<u8>) -> Result<(), WalkError> {
    let start = j.current_index();
    j.known_skip(peek)?;
    out.extend_from_slice(&data[start..j.current_index()]);
    Ok(())
}

fn project_value(
    j: &mut Jiter,
    data: &[u8],
    peek: Peek,
    spec: &Spec,
    out: &mut Vec<u8>,
) -> Result<(), WalkError> {
    match (spec, peek) {
        (Spec::Object(map), Peek::Object) => project_object(j, data, map, out),
        (Spec::Array(inner), Peek::Array) => project_array(j, data, inner, out),
        _ => copy_value(j, data, peek, out),
    }
}

/// Cursor at `{`. Keep members whose key is in `map`, each with its own spec; skip every other member.
fn project_object(
    j: &mut Jiter,
    data: &[u8],
    map: &std::collections::HashMap<String, Spec>,
    out: &mut Vec<u8>,
) -> Result<(), WalkError> {
    out.push(b'{');
    let mut sep = j.current_index();
    let mut key = j.next_object()?;
    let mut first = true;
    while let Some(k) = key {
        // decide before the next cursor call: `k` borrows jiter's tape
        let sub = map.get(k);
        let ks = key_start(data, sep);
        let peek = j.peek()?;
        match sub {
            None => j.known_skip(peek)?,
            Some(sub) => {
                if !first {
                    out.push(b',');
                }
                first = false;
                out.extend_from_slice(&data[ks..j.current_index()]); // `"key" :` including its whitespace
                project_value(j, data, peek, sub, out)?;
            }
        }
        sep = j.current_index();
        key = j.next_key()?;
    }
    out.push(b'}');
    Ok(())
}

/// Cursor at `[`. Project every element with `inner`.
fn project_array(
    j: &mut Jiter,
    data: &[u8],
    inner: &Spec,
    out: &mut Vec<u8>,
) -> Result<(), WalkError> {
    out.push(b'[');
    let mut el = j.next_array()?;
    let mut first = true;
    while let Some(peek) = el {
        if !first {
            out.push(b',');
        }
        first = false;
        project_value(j, data, peek, inner, out)?;
        el = j.array_step()?;
    }
    out.push(b']');
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn keys(names: &[&str]) -> Spec {
        Spec::Object(names.iter().map(|n| (n.to_string(), Spec::Keep)).collect())
    }

    fn run(data: &str, spec: &Spec) -> Result<String, WalkError> {
        project_bytes(data.as_bytes(), spec).map(|v| String::from_utf8(v).unwrap())
    }

    #[test]
    fn keeps_named_members_raw_and_drops_the_rest() {
        let out = run(
            r#"{"a": 1.5, "junk": [1, {"x": "y"}], "s": "str", "b": NaN}"#,
            &keys(&["a", "b"]),
        )
        .unwrap();
        assert_eq!(out, r#"{"a": 1.5,"b": NaN}"#);
    }

    #[test]
    fn drops_unknown_strings_and_scalars_too() {
        let out = run(
            r#"{"a":1,"s":"xxxx","n":null,"t":true,"z":0e0}"#,
            &keys(&["a"]),
        )
        .unwrap();
        assert_eq!(out, r#"{"a":1}"#);
    }

    #[test]
    fn keeps_every_occurrence_of_a_duplicated_key_in_order() {
        let out = run(r#"{"a": 1, "junk": {}, "a": 2, "a": [3]}"#, &keys(&["a"])).unwrap();
        assert_eq!(out, r#"{"a": 1,"a": 2,"a": [3]}"#);
    }

    #[test]
    fn matches_keys_after_unescaping_but_copies_the_original_spelling() {
        // "\u0061" is "a" after unescaping: it must match the spec key "a" and be copied as written
        let out = run(r#"{"\u0061": 1, "b": 2}"#, &keys(&["a"])).unwrap();
        assert_eq!(out, r#"{"\u0061": 1}"#);
    }

    #[test]
    fn keys_containing_separators_and_quotes() {
        let out = run(
            r#"{"x,\"y": [1, 2], "a": 1, "p,\"q": {"z": 1}}"#,
            &keys(&["x,\"y"]),
        )
        .unwrap();
        assert_eq!(out, r#"{"x,\"y": [1, 2]}"#);
    }

    #[test]
    fn nested_object_spec_and_all_over_arrays() {
        let inner = Spec::Object(HashMap::from([("id".to_string(), Spec::Keep)]));
        let spec = Spec::Object(HashMap::from([
            ("name".to_string(), Spec::Keep),
            ("items".to_string(), Spec::Array(Box::new(inner))),
        ]));
        let doc =
            r#"{"name":"n","items":[{"id":1,"big":[1,2,3]},{"id":2,"big":{}}],"meta":{"x":1}}"#;
        assert_eq!(
            run(doc, &spec).unwrap(),
            r#"{"name":"n","items":[{"id":1},{"id":2}]}"#
        );
    }

    #[test]
    fn spec_value_mismatch_copies_raw() {
        let obj_spec = Spec::Object(HashMap::from([(
            "k".to_string(),
            Spec::Object(HashMap::new()),
        )]));
        assert_eq!(
            run(r#"{"k": [1, 2]}"#, &obj_spec).unwrap(),
            r#"{"k": [1, 2]}"#
        );
        let arr_spec = Spec::Object(HashMap::from([(
            "k".to_string(),
            Spec::Array(Box::new(Spec::Keep)),
        )]));
        assert_eq!(
            run(r#"{"k": {"a": 1}}"#, &arr_spec).unwrap(),
            r#"{"k": {"a": 1}}"#
        );
        assert_eq!(run(r#"{"k": "s"}"#, &obj_spec).unwrap(), r#"{"k": "s"}"#);
    }

    #[test]
    fn empty_object_and_all_dropped_give_empty_object() {
        assert_eq!(run("{}", &keys(&["a"])).unwrap(), "{}");
        assert_eq!(run(r#"{ "a" : 1 }"#, &keys(&[])).unwrap(), "{}");
    }

    #[test]
    fn whitespace_between_key_and_value_is_preserved_for_kept_members() {
        let out = run("{\n  \"a\" : 1,\n  \"b\": 2\n}", &keys(&["a"])).unwrap();
        assert_eq!(out, "{\"a\" : 1}");
    }

    #[test]
    fn non_object_root_is_reported() {
        assert!(matches!(
            run("[1, 2]", &keys(&["a"])),
            Err(WalkError::NotAnObject)
        ));
        assert!(matches!(
            run("42", &keys(&["a"])),
            Err(WalkError::NotAnObject)
        ));
        assert!(matches!(run("", &keys(&["a"])), Err(WalkError::Json(_))));
    }

    #[test]
    fn syntax_errors_anywhere_are_reported() {
        assert!(matches!(
            run(r#"{"a": 1, "junk": [1, 2,]}"#, &keys(&["a"])),
            Err(WalkError::Json(_))
        ));
        assert!(matches!(
            run(r#"{"a": 1, "junk": {"k": tru}}"#, &keys(&["a"])),
            Err(WalkError::Json(_))
        ));
        assert!(matches!(
            run(r#"{"a": 1"#, &keys(&["a"])),
            Err(WalkError::Json(_))
        ));
        assert!(matches!(
            run(r#"{"a": 1} x"#, &keys(&["a"])),
            Err(WalkError::Json(_))
        ));
    }

    #[test]
    fn deep_nesting_inside_dropped_member_is_bounded() {
        let ok = format!(
            r#"{{"a": 1, "deep": {}0{}}}"#,
            "[".repeat(200),
            "]".repeat(200)
        );
        assert!(run(&ok, &keys(&["a"])).is_ok());
        let too_deep = format!(
            r#"{{"a": 1, "deep": {}0{}}}"#,
            "[".repeat(201),
            "]".repeat(201)
        );
        assert!(matches!(
            run(&too_deep, &keys(&["a"])),
            Err(WalkError::Json(_))
        ));
    }
}

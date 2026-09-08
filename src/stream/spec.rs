use std::collections::HashMap;

use crate::spec::Spec;

pub(super) enum Node {
    Keep,
    Discard,
    Object(HashMap<String, usize>),
    ExcludeObject(HashMap<String, usize>),
    Array(usize),
}

/// An immutable arena: sessions store node indices instead of borrowing their own plan.
pub(crate) struct StreamPlan {
    nodes: Vec<Node>,
}

impl StreamPlan {
    pub(crate) fn new(spec: &Spec) -> Self {
        let mut plan = Self { nodes: Vec::new() };
        plan.insert(spec);
        plan
    }

    fn insert(&mut self, spec: &Spec) -> usize {
        let index = self.nodes.len();
        self.nodes.push(Node::Keep);
        self.nodes[index] = match spec {
            Spec::Keep => Node::Keep,
            Spec::Discard => Node::Discard,
            Spec::Object(fields) | Spec::ExcludeObject(fields) => {
                let children = fields
                    .iter()
                    .map(|(key, value)| (key.clone(), self.insert(value)))
                    .collect();
                if matches!(spec, Spec::ExcludeObject(_)) {
                    Node::ExcludeObject(children)
                } else {
                    Node::Object(children)
                }
            }
            Spec::Array(child) => Node::Array(self.insert(child)),
        };
        index
    }

    pub(super) fn node(&self, index: usize) -> &Node {
        &self.nodes[index]
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;
    use crate::spec::Spec;

    #[test]
    fn flatten_preserves_nested_and_keep_nodes() {
        let source = Spec::Object(HashMap::from([
            ("id".into(), Spec::Keep),
            ("items".into(), Spec::Array(Box::new(Spec::Keep))),
        ]));
        let plan = StreamPlan::new(&source);
        let Node::Object(fields) = plan.node(0) else {
            panic!("object expected")
        };
        assert!(matches!(plan.node(fields["id"]), Node::Keep));
        let Node::Array(child) = plan.node(fields["items"]) else {
            panic!("array expected")
        };
        assert!(matches!(plan.node(*child), Node::Keep));
        assert_eq!(plan.nodes.len(), 4);
    }
}
